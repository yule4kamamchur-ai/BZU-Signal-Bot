"""Release contracts for causal scenario admission and execution protection."""
import unittest
import os
from dataclasses import replace
from unittest.mock import patch

import bzu_policy
import directional_policy as direction
import entry_adverse_selection as adverse
import full_limit_engine as engine
import ict_methodology as ict
import live_terminal
import market_wave as wave
import notification_report
import oil_risk
import portfolio_arbitration as arbitration
import position_monitor
import scenario_planner as scenarios
import strategy_direct_entries as direct
from test_v12_24_momentum import NOW, STEP
from test_v12_31_market_wave import snapshot
import test_v12_35_stale_and_setups as positions
from test_full_limit_engine import analysis, order


def rows(ohlc):
    return [engine.Candle(NOW-(len(ohlc)-i)*STEP,*row) for i,row in enumerate(ohlc)]


def displacement():
    return [(99.8,100.10,99.70,99.90)]*30 + [
        (99.90,100.05,99.50,99.82),
        (99.82,100.30,99.72,99.92),
        (99.72,99.94,99.42,99.80),
        (99.82,100.85,99.78,100.75),
        (100.60,100.90,100.50,100.72),
    ]


class ScenarioContracts(unittest.TestCase):
    def test_two_sides_and_hard_location_gates(self):
        snap,cfg = snapshot(100.,[(100.,103.,97.,100.)]*192,[])
        data = scenarios.build(snap,cfg,engine)
        self.assertEqual(set(data) & {'LONG','SHORT'}, {'LONG','SHORT'})
        self.assertEqual((data['range']['discount'],data['range']['premium']), (98.8,101.2))
        sideways={'state':'SIDEWAYS','trend_strength':.20}
        self.assertEqual(scenarios.entry_error(snap,cfg,engine,-1,98.5,{},trend=True,regime=sideways),
                         'BLOCKED_DEALING_RANGE_30_70')
        self.assertEqual(scenarios.entry_error(snap,cfg,engine,1,101.5,{},trend=True,regime=sideways),
                         'BLOCKED_DEALING_RANGE_30_70')
        self.assertEqual(scenarios.entry_error(snap,cfg,engine,1,100.,{},trend=False),
                         'BLOCKED_OUTSIDE_HTF_POI')
        public=live_terminal.build(engine.new_state(cfg),snap,{'analysis':{}},cfg,engine)
        self.assertEqual(public['scenario_plan']['market']['range_position_pct'],50)
        self.assertNotIn('LONG',public['scenario_plan'])
        self.assertNotIn('SHORT',public['scenario_plan'])
        report=notification_report.build_checklist(engine.new_state(cfg),snap,{},cfg,engine)
        self.assertNotIn('30/70',report)
        self.assertNotIn('А · LONG',report)
        self.assertNotIn('Б · SHORT',report)

    def test_outside_range_exception_requires_closed_retest(self):
        snap,cfg=snapshot(103.4,[(100.,103.,97.,100.)]*100+[
            (102.8,103.8,102.8,103.6),(103.6,103.8,103.0,103.4)],[])
        zone={'kind':'SR_FLIP_RECLAIM','reference':103.,
              'wave_proof':{'break_ts':NOW-STEP,'retest_ts':NOW}}
        self.assertIsNone(scenarios.entry_error(snap,cfg,engine,1,103.,zone,trend=True))
        self.assertFalse(scenarios.range_retest(replace(snap,now=NOW-1),zone,1,.01,103.))

    def test_displacement_mss_sweep_fvg_only_after_third_bar_closes(self):
        snap,cfg=snapshot(100.72,displacement(),[],htf_side=-1)
        self.assertFalse(wave.displacement_override(replace(snap,now=NOW-1),cfg.tick,engine))
        proof=wave.displacement_override(snap,cfg.tick,engine)
        self.assertEqual((proof['proof'],proof['side'],proof['known_ts']), ('DISPLACEMENT_MSS_FVG',1,NOW))
        self.assertEqual(direction.execution_regime(snap,cfg,engine)['trend_anchor_side'],1)
        candles=snap.candles['15m']
        self.assertIsNotNone(ict.breaker_evidence(candles,len(candles)-2,
            engine.pivots(candles,strength=1),1,cfg.tick))
        without_raid=[*candles]
        without_raid[-3]=replace(without_raid[-3],low=99.62)
        self.assertIsNone(ict.breaker_evidence(without_raid,len(candles)-2,
            engine.pivots(without_raid,strength=1),1,cfg.tick))
        without_gap=[*candles]
        without_gap[-1]=replace(without_gap[-1],low=99.80)
        self.assertIsNone(ict.breaker_evidence(without_gap,len(candles)-2,
            engine.pivots(without_gap,strength=1),1,cfg.tick))

    def test_adaptive_depth_uses_closed_impulse(self):
        snap,cfg=snapshot(100.72,displacement(),[])
        zone={'kind':'FVG_PULLBACK','low':99.94,'high':100.50,'timeframe':'15m',
              'wave_proof':{'impulse_ts':NOW-2*STEP}}
        self.assertEqual(direct.adaptive_limit(snap,zone,{},1,.01,engine),
                         (100.50,'STRONG_DISPLACEMENT'))
        normal={**zone,'wave_proof':{'impulse_ts':NOW-8*STEP}}
        self.assertEqual(direct.adaptive_limit(snap,normal,{},1,.01,engine),
                         (100.22,'NORMAL_ROTATION_50'))

    def test_velocity_hold_and_release_and_market_distance(self):
        ohlc=[(100.4,100.52,100.32,100.4)]*31 + [
            (100.57,100.60,99.95,99.97),(99.98,100.20,99.96,100.15)]
        snap,cfg=snapshot(100.15,ohlc,[])
        zone={'low':99.94,'high':100.05,'known_ts':NOW-5*STEP}
        before=replace(snap,now=NOW-STEP,price=99.97)
        p={'entry_zone':zone,'side':1,'entry':100.}
        self.assertTrue(engine.hold_pending_approach(p,before,cfg))
        self.assertEqual(p['entry_hold'],'CONFIRMATION_REQUIRED')
        self.assertFalse(engine.hold_pending_approach(p,snap,cfg))
        trigger=adverse.rejection_trigger(snap,1,zone,100.,1.0,100.16,.01)
        self.assertEqual(trigger['pattern'],'M15_LEVEL_REJECTION')
        self.assertIsNone(adverse.rejection_trigger(snap,1,zone,100.,1.,100.21,.01))
        self.assertIsNone(adverse.rejection_trigger(replace(snap,now=NOW-1,ticker_ts=NOW-1),
                                                   1,zone,100.,1.,100.16,.01))

    def test_sr_flip_preplans_first_retest_symmetrically(self):
        ohlc=[(90.24,90.39,90.19,90.28)]*81
        ohlc[21]=(90.24,90.70,90.19,90.28)
        ohlc[-3]=(90.24,90.60,90.19,90.60)
        ohlc[-2]=(90.60,91.19,90.60,91.12)
        ohlc[-1]=(91.12,91.14,90.70,91.08)
        for side in (1,-1):
            bars=rows(ohlc if side==1 else [(200-o,200-l,200-h,200-c) for o,h,l,c in ohlc])
            level=90.70 if side==1 else 109.30
            for visible in (bars[:-1],bars):
                hits=[z for z in wave._reclaim_breakers(visible,.01,engine,proactive=True)
                      if z['side']==side and abs(z['reference']-level)<1e-8]
                self.assertTrue(hits)
                proof=hits[-1]['wave_proof']
                self.assertEqual(hits[-1]['kind'],'SR_FLIP_RECLAIM')
                self.assertEqual(proof.get('retest_ts'),NOW if visible==bars else None)

    def test_obstacle_overrides_a_theoretical_target(self):
        levels=[{'side':-1,'level':101.2,'kind':'1H_SWING_HIGH'},{'side':-1,'level':103.0}]
        obstacle, room=oil_risk.structural_room(1,100.,1.,levels)
        self.assertEqual(obstacle,levels[0])
        self.assertLess(room,1.35)
        self.assertEqual(oil_risk.entry_tp1_error(1,100.,1.,101.39),'BLOCKED_LOW_RR_135')
        self.assertIsNone(oil_risk.entry_tp1_error(1,100.,1.,101.40))
        snap,cfg=snapshot(100.,[(100.,103.,97.,100.)]*100,[])
        with patch.object(scenarios,'structure_levels',return_value=levels), \
             patch.object(engine,'full_targets',return_value=[]):
            plan,reason=bzu_policy.protected_targets({},snap,type('A',(),{'side':'LONG'})(),cfg,
                engine,100.,99.,1,.25,False)
        self.assertIsNone(plan)
        self.assertEqual(reason,'BLOCKED_NO_ROOM_TO_STRUCTURE')

    def test_engulfed_entry_exits_and_sar_scans_same_cycle(self):
        snap,cfg=snapshot(100.18,[(99.94,100.05,99.85,99.98)]*95+[(99.92,100.24,99.90,100.18)],[])
        p,book,_=positions.PositionGuard().make_position(side=-1,entry=100.,risk=1.5,entered=NOW-4*STEP)
        p['entry_zone']={'low':99.96,'high':100.06}
        p['last_bar']=NOW-180_000
        cfg=replace(cfg,include_commission=False)
        book['config_hash']=engine.new_state(cfg)['config_hash']
        with patch.object(engine,'advance_lifecycle'):
            result=engine.run_cycle(book,snap,cfg)
        trade,=book['trades']
        self.assertEqual(trade['close_reason'],'CLOSED_ADVERSE_STRUCTURE_SHIFT')
        self.assertEqual(result['analysis']['stop_and_reverse']['side'],1)
        self.assertEqual(result['analysis']['stop_and_reverse']['closed_order_id'],p['id'])
        self.assertIsNone(arbitration.stop_and_reverse([],book['trades']))

    def test_sar_reserves_a_valid_opposite_limit_in_same_cycle(self):
        snap,cfg=snapshot(100.18,[(99.94,100.05,98.60,99.98)]*95+[(99.92,100.24,99.90,100.18)],[])
        p,book,_=positions.PositionGuard().make_position(side=-1,entry=100.,risk=1.5,entered=NOW-4*STEP)
        p.update(entry_zone={'low':99.96,'high':100.06},last_bar=NOW-180_000)
        cfg=replace(cfg,include_commission=False)
        book['config_hash']=engine.new_state(cfg)['config_hash']
        long=order(1)
        long.update(id='fresh-long',event_key='fresh-long',zone_key='fresh-zone',setup='BREAKOUT_RETEST',
                    placed_ts=NOW,expires_ts=NOW+STEP,entry=98.60,stop=97.60,initial_stop=97.60,risk=1.,
                    tp1=100.,tp2=100.55,tp=100.55,score=85.,direction_score=85.,
                    trend_rel='WITH_TREND',action_type='NEW_PENDING_LIMIT',order_type='LIMIT',
                    policy=bzu_policy.POLICY,exit_policy=bzu_policy.EXIT,organic_risk={'model':oil_risk.MODEL},
                    entry_zone={'kind':'SR_FLIP_RECLAIM','timeframe':'15m','low':98.55,'high':98.65})
        assess={'score':85.,'tactic':'LIMIT','entry_condition':'RETEST'}
        with patch.object(engine,'advance_lifecycle'), \
             patch.object(engine,'analyze',return_value=analysis([long])), \
             patch.object(engine.scanner,'calculate_zone_trade_plan',return_value=assess), \
             patch.object(direction,'execution_regime',return_value={'state':'BULLISH_ACTIVE'}), \
             patch.dict(os.environ,{'BZU_ENTRY_MODE':'HYBRID','BZU_EDGE_MODE':'SHADOW'}):
            result=engine.run_cycle(book,snap,cfg)
        self.assertEqual(result['analysis']['stop_and_reverse']['outcome'],'ORDER_SELECTED')
        self.assertEqual(result['action'],'PLACE_LIMIT')
        self.assertEqual([p['id'] for p in engine.pending_plans(book)],['fresh-long'])
        self.assertEqual([ev['kind'] for ev in result['events'] if ev['kind'] in ('CLOSED','PLACED')],
                         ['CLOSED','PLACED'])

    def test_lockout_covers_any_loss_or_adverse_exit(self):
        snap,cfg=snapshot(100.,[(100.,100.3,99.7,100.)]*100,[])
        for reason, net in [('ROUND_TRIP_EXIT',-.01),('CLOSED_ADVERSE_STRUCTURE_SHIFT',.02)]:
            t={'side':-1,'entry':100.,'net_r':net,'close_reason':reason,'closed_ts':NOW-STEP}
            self.assertEqual(bzu_policy.adverse_reentry_reason(-1,[t],snap,cfg,engine),
                             'ADVERSE_STRUCTURE_LOCKOUT')
            self.assertIsNone(bzu_policy.adverse_reentry_reason(1,[t],snap,cfg,engine))
            self.assertIsNone(bzu_policy.adverse_reentry_reason(-1,[t],
                replace(snap,now=NOW+15*STEP),cfg,engine))


if __name__=='__main__':
    unittest.main()
