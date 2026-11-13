"""Contracts spanning the risk engine, scanner, dashboard and paper ledger."""
from dataclasses import replace
import unittest
from unittest.mock import patch

import bzu_policy
import entry_adverse_selection
import full_limit_engine as engine
import oil_risk
import portfolio_arbitration
import position_monitor
import scenario_planner
import strategy_direct_entries
from test_full_limit_engine import order
from test_v12_24_momentum import NOW, STEP
import test_v12_35_stale_and_setups as position_fixtures
from test_v12_40_execution import base_fixture


class UnifiedExecution(unittest.TestCase):
    def test_preview_and_engine_targets_ignore_soft_local_swing(self):
        snap,cfg = base_fixture()
        entry,stop = 93.69,92.81
        risk = entry-stop
        local = {'side':-1,'level':94.12,'kind':'15m_SWING_HIGH'}
        with patch.object(engine,'full_targets',return_value=[]), \
             patch.object(oil_risk,'stop_anchors',return_value=[]), \
             patch.object(oil_risk,'snapshot_profile',return_value={'atr_15m':.20}):
            preview = scenario_planner._preview({'side':1,'entry_hint':entry,'low':92.93,'high':entry},
                                                snap,cfg,engine,[local])
            geometry = engine.scanner.organic_targets(entry,risk,1,[],snap,cfg.tick,engine,trend=True)
        self.assertEqual(preview['stop'],stop)
        self.assertEqual(preview['tp1'],geometry['tp1'])
        self.assertGreaterEqual(preview['rr1'],1.45)
        self.assertEqual(preview['tp1'],94.97)
        self.assertEqual(oil_risk.structural_room(1,entry,risk,[local],trend=True),(None,None))

    def test_tolerated_touch_fills_entire_paper_leg(self):
        _,cfg = base_fixture()
        for side in (1,-1):
            book = engine.new_state(cfg)
            p=order(side,placed=NOW)
            p.update(policy=bzu_policy.POLICY,execution='PAPER_SIGNAL',organic_risk={'model':oil_risk.MODEL},
                     tp1=100+side*1.45,tp2=100+side*2,action_type='NEW_PENDING_LIMIT',
                     entry_legs=[{'name':'L1','price':100,'fraction':1.,'status':'PENDING'}],
                     effective_ts=NOW)
            book['pending']=p
            price=100+side*.05
            bar=engine.Candle(NOW,price,price,price,price)
            self.assertTrue(engine.paper_limit_touch(p,bar,cfg))
            snap=engine.Snapshot(NOW+60_000,price,{'1m':[bar]})
            engine._advance_slot_lifecycle(book,snap,cfg,entry_gates_checked=True)
            self.assertEqual(book['active']['filled_fraction'],1.)
            self.assertEqual(book['orders'][-1]['reason'],'PAPER_TOUCH_TOLERANCE')
            far=replace(bar,low=100.06,high=100.06) if side==1 else replace(bar,low=99.94,high=99.94)
            self.assertFalse(engine.paper_limit_touch(p,far,cfg))

    def test_reanchor_uses_new_base_before_keep_trend_branch(self):
        snap,cfg=base_fixture()
        snap=replace(snap,price=94.10)
        incumbent={**order(1,placed=NOW-4*STEP,expiry=NOW+STEP),'entry':92.97,'trend_rel':'WITH_TREND'}
        challenger={**order(1,placed=NOW),'id':'new','event_key':'new','entry':93.68,
                    'quality':{'accepted':True},
                    'entry_zone':{'kind':'CONSOLIDATION_BREAKOUT_BASE','known_ts':NOW}}
        audit=portfolio_arbitration.decision([incumbent],[challenger],snap,cfg,engine.policy,engine=engine)
        self.assertEqual((audit['decision'],audit['reason'],audit['candidate_id']),
                         ('REQUEST_REPLACEMENT','REPLACED_BY_CLOSER_SETUP','new'))

    def test_adverse_exit_waits_45_minutes_and_needs_drawdown(self):
        p,_,cfg=position_fixtures.PositionGuard().make_position(entry=93.69,risk=.88)
        p.update(protection_structure_ts=NOW+STEP,protection_structure_side=-1,
                 protection_structure_timeframe='15m')
        self.assertFalse(position_monitor.evaluate_active_trade_lifecycle(p,NOW+2*STEP,93.60,cfg,engine)['should_exit'])
        self.assertFalse(position_monitor.evaluate_active_trade_lifecycle(p,NOW+3*STEP,93.60,cfg,engine)['should_exit'])
        self.assertEqual(position_monitor.evaluate_active_trade_lifecycle(p,NOW+3*STEP,93.37,cfg,engine)['exit_reason'],
                         'CLOSED_ADVERSE_STRUCTURE_SHIFT')

    def test_impulse_uses_shallow_body_retest(self):
        snap,cfg=base_fixture()
        bar=engine.Candle(NOW-STEP,92.72,93.66,92.71,93.64)
        snap=replace(snap,price=93.64,candles={**snap.candles,'15m':[*snap.candles['15m'][:-1],bar]})
        zone={'kind':'SWEEP_CONTINUATION','known_ts':NOW,'timeframe':'15m',
              'low':92.71,'high':93.06}
        entry,basis=strategy_direct_entries.adaptive_limit(snap,zone,{},1,cfg.tick,engine)
        self.assertEqual((entry,basis),(93.39,'SHALLOW_M15_RETEST'))

    def test_market_uses_wider_atr_window_but_rejects_chase(self):
        snap,cfg=base_fixture()
        bars=[engine.Candle(NOW-(20-i)*STEP,93.70,94.04,93.36,93.70) for i in range(20)]
        bars[-1]=engine.Candle(NOW-STEP,93.76,94.08,93.74,94.06)
        snap=replace(snap,price=94.06,ask=94.07,candles={**snap.candles,'15m':bars})
        zone={'kind':'SR_FLIP_RECLAIM','low':93.67,'high':93.82,'reference':93.80,
              'known_ts':NOW-STEP,'wave_proof':{'break_ts':NOW-STEP}}
        prove=lambda quote:entry_adverse_selection.trend_zone_market_trigger(
            snap,1,zone,.85,quote,cfg.tick,engine)
        self.assertGreater(prove(94.07)['max_distance'],.27)
        self.assertIsNone(prove(94.13))
