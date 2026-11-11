"""Check causal regime, entry and lifecycle changes through the paper engine."""
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo
import json
import os
import unittest
from unittest.mock import patch

import bzu_policy
import directional_policy as direction
import full_limit_engine as engine
import ict_methodology as ict
import position_monitor
import market_wave as wave
import setup_scanner as scanner
import test_v12_35_stale_and_setups as positions
from test_v12_24_momentum import NOW, STEP
from test_v12_31_market_wave import snapshot
from test_v12_32_organic_oil import swing_fixture
from test_v12_36_reversal_8850 import ENV, REGIME


def kyiv(hour, minute):
    return int(datetime(2026, 11, 11, hour, minute, tzinfo=ZoneInfo('Europe/Kyiv')).timestamp()*1000)


class StructureSync(unittest.TestCase):
    def test_m15_bos_switches_at_75_percent_adr_and_survives_retest(self):
        snap, cfg, _ = swing_fixture()
        slow = {'state': 'BULLISH_ACTIVE', 'side_1h': -1, 'side_4h': -1,
                'active_structure': {'side': 1}, 'local_structure': {'side': 1}}
        with patch.object(direction, 'market_regime', return_value=slow):
            before = direction.execution_regime(replace(snap, now=NOW-1), cfg, engine)
            at_break = direction.execution_regime(snap, cfg, engine)
            self.assertEqual(before['state'], 'BEARISH_ACTIVE')
            self.assertEqual(at_break['state'], 'BULLISH_ACTIVE')
            proof = at_break['structure_sync']
            self.assertEqual((proof['break_ts'], proof['level']), (NOW, 96.))
            self.assertGreaterEqual(proof['wave_adr'], .75)
            pullback = engine.Candle(NOW, 96.28, 96.30, 95.91, 95.97)
            candles = {**snap.candles, '15m': [*snap.candles['15m'], pullback]}
            retest = replace(snap, now=NOW+STEP, price=95.97, candles=candles)
            self.assertEqual(direction.execution_regime(retest, cfg, engine)['state'], 'BULLISH_ACTIVE')
            broken = engine.Candle(NOW+STEP, 95.97, 96.01, 95.40, 95.45)
            candles = {**candles, '15m': [*candles['15m'], broken]}
            invalidated = replace(retest, now=NOW+2*STEP, price=95.45, candles=candles)
            self.assertEqual(direction.execution_regime(invalidated, cfg, engine)['state'], 'BEARISH_ACTIVE')

    def test_reclaim_can_find_a_level_older_than_36_bars(self):
        rows = [engine.Candle(NOW-(81-i)*STEP, 90.24, 90.39, 90.19, 90.28)
                for i in range(81)]
        rows[21] = replace(rows[21], high=90.70)
        rows[-3] = replace(rows[-3], high=90.60, close=90.60)
        rows[-2] = replace(rows[-2], open=90.60, high=91.19, low=90.60, close=91.12)
        rows[-1] = replace(rows[-1], open=91.12, high=91.14, low=90.70, close=91.08)
        zone, = [z for z in wave._reclaim_breakers(rows, .01, engine)
                 if z['side'] == 1 and z['known_ts'] == NOW and z['reference'] == 90.70]
        self.assertGreater(NOW-zone['wave_proof']['reference_known_ts'], 36*STEP)
        self.assertFalse(any(z['reference'] == 90.70 for z in wave._reclaim_breakers(rows[:-1], .01, engine)))
        snap = engine.Snapshot(NOW, 91.08, {'15m': rows})
        self.assertTrue(direction.reversal_sweep_override(1, zone, snap, .01))
        self.assertFalse(direction.reversal_sweep_override(1, zone, replace(snap, price=90.50), .01))
        market, cfg = snapshot(91.08, [(c.open, c.high, c.low, c.close) for c in rows], [], htf_side=-1)
        targets = [{'level': 92.78, 'kind': '1H_LIQUIDITY', 'timeframe': '1H', 'known_ts': NOW-STEP}]
        book = engine.new_state(cfg)
        with patch.object(direction, 'execution_regime', return_value=REGIME), \
             patch.object(engine, 'full_targets', return_value=targets), patch.dict(os.environ, ENV):
            result = engine.run_cycle(book, market, cfg)
        self.assertEqual(result['action'], 'PLACE_LIMIT', result['analysis'].get('rejections'))
        plan, = engine.pending_plans(book)
        self.assertEqual((plan['side'], plan['entry'], plan['source_strategy']), (1, 90.70, 'RECLAIM_BREAKER'))
        self.assertGreaterEqual(plan['score'], 84)
        self.assertGreaterEqual(plan['rr1'], 1.35)

    def test_archive_does_not_contain_confirmed_9070_retest_at_1100(self):
        # First-observed OHLC copied from research_market_v12_6.json.
        rows = [engine.Candle(kyiv(h, m), *ohlc) for (h, m), ohlc in [
            ((10, 30), (90.72, 90.83, 90.36, 90.55)),
            ((10, 45), (90.55, 90.71, 90.45, 90.47)),
            ((11, 0), (90.45, 90.58, 90.19, 90.51)),
            ((11, 15), (90.51, 91.52, 90.51, 91.50)),
        ]]
        before = engine.Snapshot(kyiv(11, 1), 90.27, {'15m': rows})
        self.assertEqual(direction.closed(before, '15m')[-1].close, 90.47)
        self.assertTrue(ict.reclaim_break(rows, 3, 1, 90.70, .01, .30))
        self.assertIsNone(wave._confirmed_retest_zone(rows, 3, 1, 90.70, kyiv(10,45), .01, .30))

    def test_adverse_exit_lockout_survives_journal_reload_and_is_symmetric(self):
        _, cfg, _ = swing_fixture()
        close = kyiv(14, 15)
        short = {'closed_ts': close, 'close_reason': 'CLOSED_ADVERSE_STRUCTURE_SHIFT',
                 'side': -1, 'entry': 90.71, 'net_r': -.49618}
        book = json.loads(json.dumps({'trades': [short, {**short, 'closed_ts': kyiv(19, 0), 'entry': 80.}]}))
        snap = engine.Snapshot(kyiv(16, 1), 91.25, {})
        gate = lambda side, s=snap: bzu_policy.adverse_reentry_reason(side, book['trades'], s, cfg, engine)
        self.assertEqual(gate(-1), 'ADVERSE_STRUCTURE_LOCKOUT')
        self.assertIsNone(gate(1))
        self.assertEqual(gate(-1, replace(snap, price=90.65)), 'ADVERSE_STRUCTURE_LOCKOUT')
        self.assertIsNone(gate(-1, replace(snap, now=close+16*STEP)))
        book['trades'].append({**short, 'side': 1, 'close_reason': 'CLOSED_EARLY_IMPULSE_FAILURE'})
        self.assertEqual(gate(1), 'ADVERSE_STRUCTURE_LOCKOUT')
        book['trades'] = [{**short, 'close_reason': 'STOP'}]
        self.assertEqual(gate(-1), 'ADVERSE_STRUCTURE_LOCKOUT')
        book['trades'] = [{**short, 'net_r': .01}]
        self.assertIsNone(gate(-1))

    def test_unlock_needs_closed_bos_and_recovery_of_entry(self):
        _, cfg, _ = swing_fixture()
        close = kyiv(14, 15)
        bars = [(91.08,91.20,91.03,91.10)]*7 + [
            (91.10,91.15,90.90,91.01), (91.01,91.14,90.96,91.05),
            (91.05,91.06,90.73,90.77), (90.77,90.85,90.54,90.64)]
        rows = [engine.Candle(close+(i-7)*STEP,*r) for i,r in enumerate(bars)]
        loss = {'side': -1, 'entry': 90.71, 'closed_ts': close, 'net_r': -.5, 'close_reason': 'STOP_GAP'}
        snap = engine.Snapshot(close+4*STEP, 90.64, {'15m': rows})
        self.assertIsNone(bzu_policy.adverse_reentry_reason(-1, [loss], snap, cfg, engine))
        early = replace(snap, now=snap.now-1)
        self.assertEqual(bzu_policy.adverse_reentry_reason(-1, [loss], early, cfg, engine),
                         'ADVERSE_STRUCTURE_LOCKOUT')

    def test_breaker_gate_requires_both_higher_lows_and_higher_highs_above_emas(self):
        _, cfg, _ = swing_fixture()
        rows = [(89.2,89.32,89.1,89.20)]*50 + [
            (89.30,90.1,89.22,90.0), (90.34,90.60,90.30,90.5),
            (90.5,90.52,90.20,90.32), (90.32,90.90,90.30,90.80),
            (90.80,91.10,90.70,91.0), (91.,91.02,90.63,90.76),
            (90.76,91.00,90.75,90.88)]
        candles = [engine.Candle(NOW-(len(rows)-i)*STEP, *r) for i,r in enumerate(rows)]
        snap = engine.Snapshot(NOW,90.88,{'15m':candles})
        p = {'side':-1,'entry':90.71,'source_strategy':'BREAKER_BLOCK',
             'entry_zone':{'low':90.68,'high':90.75}}
        self.assertEqual(bzu_policy.breaker_structure_error(p,snap,cfg,engine), 'M15_STRUCTURE_OPPOSES_ENTRY')
        self.assertIsNone(bzu_policy.breaker_structure_error(p,replace(snap,price=89.20),cfg,engine))
        flat_high = [*candles]
        flat_high[-6] = replace(flat_high[-6], high=91.20)
        self.assertIsNone(bzu_policy.breaker_structure_error(
            p,replace(snap,candles={'15m':flat_high}),cfg,engine))

    def test_breaker_cannot_bypass_the_tp1_floor_at_router_or_waiting_limit(self):
        snap, cfg, _ = swing_fixture()
        plan = {'id': 'short-189', 'side': -1, 'entry': 91.25, 'stop': 93.26, 'risk': 2.01,
                'tp1': 89.03, 'tp2': 87.4, 'low': 91.25, 'high': 91.40,
                'kind': 'BREAKER_BLOCK', 'source_strategy': 'BREAKER_BLOCK',
                'score': 81, 'score_source': 'CONTEXT'}
        for kind, family in (('BREAKER_BLOCK','BREAKER_BLOCK'), ('SUPPLY_OB','ORDER_BLOCK'),
                             ('FVG_BEAR','FAIR_VALUE_GAP'), ('CONTINUATION_PULLBACK','CONTINUATION_PULLBACK')):
            self.assertEqual(scanner.candidate_plan_error({**plan, 'kind':kind, 'source_strategy':family}),
                             'BLOCKED_LOW_RR_135')
        book = engine.new_state(cfg)
        book['pending'] = plan
        with patch.object(engine, 'request_cancel') as cancel:
            engine.review_waiting_conditions(book, plan, snap, cfg)
        cancel.assert_called_once_with(book, plan, 'BLOCKED_LOW_RR_135', snap.now)

    def test_short_189_exits_at_observed_price_without_three_hour_delay(self):
        test = positions.PositionGuard()
        position, book, cfg = test.make_position(side=-1, entry=91.25, risk=2.01)
        ts = NOW + 4 * STEP
        rows = [engine.Candle(ts-(9-i)*STEP, 91.94, 92.01, 91.91, 91.96)
                for i in range(9)]
        snap = engine.Snapshot(ts, 91.96, {'15m': rows}, 91.96, 91.97, ticker_ts=ts)
        with patch.object(direction, 'execution_regime', return_value={'state': 'BULLISH_ACTIVE'}):
            engine.check_quote_exits(book, snap, cfg)
        self.assertIsNone(book['active'])
        trade, = book['trades']
        self.assertEqual((trade['exit'], trade['close_reason'], trade['bars_in_trade']),
                         (91.97, 'CLOSED_ADVERSE_STRUCTURE_SHIFT', 4))

    def test_confirmed_m15_break_exits_even_above_the_drawdown_floor(self):
        test = positions.PositionGuard()
        position, book, cfg = test.make_position()
        end = NOW+4*STEP
        rows = [engine.Candle(end-(8-i)*STEP, 98.40, 98.60, low, close)
                for i, (low, close) in enumerate([(98.22, 98.45)]*4 + [
                    (98.10, 98.44), (98.23, 98.47), (98.24, 98.48), (97.97, 98.02)])]
        snapshot = engine.Snapshot(end, 98.02, {'15m': rows})
        test.bar(book, cfg, end-60_000, 98.40, 98.42, 97.97, 98.02, snapshot)
        self.assertIs(book['active'], position)
        test.bar(book, cfg, end, 98.02, 98.12, 97.97, 98.02, snapshot)
        trade, = book['trades']
        self.assertEqual((trade['close_reason'], trade['protection_structure_timeframe']),
                         ('CLOSED_ADVERSE_STRUCTURE_SHIFT', '15m'))
        self.assertGreater(trade['gross_r'], -.35)

    def test_market_early_impulse_failure_closes_through_execution(self):
        entered = kyiv(16, 1)
        end = kyiv(17, 0)
        test = positions.PositionGuard()
        p, book, cfg = test.make_position(side=-1, entry=91.25, risk=2.01, entered=entered)
        p['order_type'] = 'MARKET'
        rows = [engine.Candle(end-(15-i)*STEP, 91.24, 91.38, 91.20, 91.28) for i in range(15)]
        rows[-1] = replace(rows[-1], open=91.74, high=92.30, low=91.71, close=92.24)
        snap = engine.Snapshot(end, 92.24, {'15m': rows}, 92.23, 92.24, ticker_ts=end)
        with patch.object(direction, 'execution_regime', return_value={'state': 'BULLISH_ACTIVE'}):
            engine.check_quote_exits(book,snap,cfg)
        trade, = book['trades']
        self.assertEqual((trade['close_reason'], trade['exit']), ('CLOSED_EARLY_IMPULSE_FAILURE', 92.24))
        self.assertEqual(trade['bars_in_trade'], 3)
        for changes in ({'order_type':'LIMIT'}, {'max_favorable_r':.15}, {'adverse_impulse_ts':end+STEP}):
            eligible = {**p, **changes}
            decision = position_monitor.evaluate_active_trade_lifecycle(eligible, end, 92.24, cfg, engine)
            self.assertNotEqual(decision['exit_reason'], 'CLOSED_EARLY_IMPULSE_FAILURE')


if __name__ == '__main__':
    unittest.main()
