"""Causal bearish breakdown, execution distance, and sweep recovery."""
import os
import unittest
from dataclasses import replace
from unittest.mock import patch

import bzu_policy
import directional_policy as direction
import entry_adverse_selection as adverse
import full_limit_engine as engine
import ict_methodology as ict
import market_wave as wave
import scenario_planner as scenarios
from test_v12_24_momentum import NOW, STEP
from test_v12_31_market_wave import snapshot

ENV = {'BZU_ENTRY_MODE':'HYBRID','BZU_EDGE_MODE':'SHADOW','BZU_ENTRY_PLAYBOOK_MODE':'STRICT'}


def breakdown_fixture(price=91.93):
    rows = [(91.33,91.45,91.25,91.36)]*100 + [
        (91.36,91.98,91.32,91.90),(91.90,92.26,91.89,92.20)] + [
        (92.21,92.33,92.10,92.25)]*13 + [
        (92.24,92.33,92.00,92.22),(92.22,92.30,92.15,92.25),
        (92.25,92.35,92.12,92.23),(92.23,92.25,91.86,91.93)]
    return snapshot(price,rows,[],htf_side=1)


def targets(context, snap, side, entry, cfg):
    risk = 93.45-entry
    return [{'level':entry-1.42*risk-.01,'kind':'1H_LIQUIDITY','timeframe':'1H','known_ts':NOW-STEP},
            {'level':entry-1.90*risk-.01,'kind':'4H_LIQUIDITY','timeframe':'4H','known_ts':NOW-STEP}]


class BreakdownCapture(unittest.TestCase):
    def test_short_breaks_a_known_swing_before_ema50_turns(self):
        snap,cfg=breakdown_fixture()
        self.assertFalse(wave.breakdown_zones(replace(snap,now=NOW-1),cfg.tick,engine))
        zone,=wave.breakdown_zones(snap,cfg.tick,engine)
        proof=zone['wave_proof']
        self.assertEqual((zone['kind'],zone['reference'],proof['mss_level']),('BREAKDOWN_RECLAIM',92.,92.))
        self.assertLess(proof['ema50'],snap.price)
        self.assertGreater(proof['ema20'],snap.price)
        self.assertGreaterEqual(proof['body_atr'],1.15)
        self.assertEqual(direction.execution_regime(snap,cfg,engine)['state'],'BEARISH_ACTIVE')
        weak=[*snap.candles['15m']]
        weak[-1]=replace(weak[-1],open=92.11)
        self.assertFalse(wave.breakdown_zones(replace(snap,candles={**snap.candles,'15m':weak}),cfg.tick,engine))

    def test_confirmed_breakdown_market_or_micro_retest_through_router(self):
        levels=[{'side':1,'kind':'1H_SWING_LOW','level':89.60,'known_ts':NOW-STEP}]
        for price, action, kind, entry in ((91.93,'ACTIVE_ENTRY','MARKET',91.92),
                                          (91.80,'PLACE_LIMIT','LIMIT',92.00),
                                          (91.20,'WATCH',None,None)):
            with self.subTest(price=price), patch.object(scenarios,'structure_levels',return_value=levels), \
                 patch.object(engine,'full_targets',side_effect=targets), patch.dict(os.environ,ENV):
                snap,cfg=breakdown_fixture(price)
                book=engine.new_state(cfg)
                result=engine.run_cycle(book,snap,cfg)
                if kind:
                    self.assertEqual(result['action'],action)
                    plan,=engine.open_plans(book)
                    self.assertEqual(plan['order_type'],kind)
                    if kind=='MARKET' or plan['source_strategy']=='BREAKDOWN_RECLAIM':
                        self.assertEqual((plan['source_strategy'],plan['entry']),('BREAKDOWN_RECLAIM',entry))
                    else:
                        self.assertEqual(plan['source_strategy'],'BOS_CONTINUATION')
                    self.assertGreaterEqual(plan['rr1'],1.40)
                    if kind=='MARKET':
                        self.assertEqual(plan['market_trigger']['pattern'],'M15_BREAKDOWN_MOMENTUM')
                else:
                    self.assertNotEqual(result['action'],'ACTIVE_ENTRY')
                    self.assertFalse(any(p['source_strategy']=='BREAKDOWN_RECLAIM' for p in engine.open_plans(book)))
                    self.assertIn('DISTANT_BREAKDOWN_RETEST',result['analysis']['rejections'])

    def test_stop_sweep_unlocks_only_after_closed_mss(self):
        snap,cfg=breakdown_fixture()
        rows=[*snap.candles['15m']]
        rows[-2]=replace(rows[-2],high=93.28)
        snap=replace(snap,candles={**snap.candles,'15m':rows})
        trade={'side':-1,'entry':91.25,'closed_ts':NOW-2*STEP+60_000,'close_reason':'STOP',
               'net_r':-1.,'stop_at_close':93.26}
        self.assertGreater(snap.price,trade['entry'])
        with patch.object(direction,'bos_recovers_level',return_value=False):
            self.assertEqual(bzu_policy.adverse_reentry_reason(-1,[trade],replace(snap,now=NOW-1),cfg,engine),
                             'ADVERSE_STRUCTURE_LOCKOUT')
            self.assertIsNone(bzu_policy.adverse_reentry_reason(-1,[trade],snap,cfg,engine))
            self.assertEqual(bzu_policy.adverse_reentry_reason(-1,[{**trade,'stop_at_close':93.60}],snap,cfg,engine),
                             'ADVERSE_STRUCTURE_LOCKOUT')
        self.assertIsNone(ict.stop_sweep_recovery(rows[:-1],-1,trade['closed_ts'],93.26,cfg.tick,engine))
        between = [replace(rows[-2],ts=NOW+i*STEP-STEP,high=92.34) for i in range(4)]
        delayed = [*rows[:-1], *between, replace(rows[-1],ts=NOW+3*STEP)]
        self.assertIsNone(ict.stop_sweep_recovery(delayed,-1,trade['closed_ts'],93.26,cfg.tick,engine))

    def test_expired_market_and_pending_retest_are_not_reactivated(self):
        snap,cfg=breakdown_fixture()
        zone,=wave.breakdown_zones(snap,cfg.tick,engine)
        old=replace(snap,now=NOW+2*STEP,ticker_ts=NOW+2*STEP)
        self.assertIsNone(adverse.breakdown_market_trigger(old,zone,92.,1.45,91.92,cfg.tick))
        self.assertEqual(engine.pending_scenario_error({'side':-1,'entry':92.,'risk':1.45,'entry_zone':zone},
                                                       old,cfg),'BREAKDOWN_EXPIRED')
        bad=replace(snap,price=91.2,bid=91.19)
        self.assertEqual(engine.pending_scenario_error({'side':-1,'entry':92.,'risk':1.45,'entry_zone':zone},
                                                       bad,cfg),'DISTANT_BREAKDOWN_RETEST')

    def test_breakdown_does_not_bypass_structural_room(self):
        snap,cfg=breakdown_fixture()
        nearby=[{'side':1,'kind':'1H_SWING_LOW','level':91.4,'known_ts':NOW-STEP,'hard_barrier':True}]
        with patch.object(scenarios,'structure_levels',return_value=nearby), \
             patch.object(engine,'full_targets',side_effect=targets), patch.dict(os.environ,ENV):
            book=engine.new_state(cfg)
            result=engine.run_cycle(book,snap,cfg)
        self.assertFalse(engine.open_plans(book))
        self.assertIn('BLOCKED_NO_ROOM_TO_STRUCTURE',result['analysis']['rejections'])


if __name__=='__main__':
    unittest.main()
