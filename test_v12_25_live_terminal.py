"""Release coverage for the production entry, exit and public-state paths."""
from dataclasses import replace
from pathlib import Path
import unittest
from unittest.mock import patch

import bot_oneshot as core
import bzu_policy
import directional_policy as direction
import entry_adverse_selection as adverse
import full_limit_engine as engine
import ict_methodology as ict
import live_terminal
import portfolio_arbitration as arbitration
import setup_scanner as scanner
import signal_quality as quality
import strategy_organics as organic
from test_v12_24_momentum import NOW, STEP, anchor_for, fixture, target_for


class EntryTests(unittest.TestCase):
    def test_one_right_bar_then_first_displacement_reaches_portfolio(self):
        for side in (1, -1):
            with self.subTest(side=side):
                snap, cfg, _ = fixture(side)
                rows = snap.candles['15m']
                rows[-3] = replace(rows[-5], ts=rows[-3].ts)
                rows[-5] = replace(rows[-4], ts=rows[-5].ts)
                self.assertEqual(list(organic.body_breaks(rows, cfg.tick, engine))[-1],
                                 (len(rows)-1, side, 74.50 if side == 1 else 75.50))
                formations = {item['family']:item for item in organic.formations(snap, cfg.tick, engine)}
                self.assertEqual(set(formations), scanner.ORGANIC_MOMENTUM)
                self.assertFalse(organic.formations(replace(snap, now=NOW-1), cfg.tick, engine))
                state = engine.new_state(cfg)
                with patch.object(engine, 'full_targets', return_value=target_for(side)):
                    result = engine.run_cycle(state, snap, cfg)
                self.assertEqual(result['action'], 'PLACE_LIMIT', result['analysis'].get('rejections'))
                plans = engine.pending_plans(state)
                self.assertTrue(any(p['source_strategy'] in scanner.ORGANIC_MOMENTUM for p in plans))
                for plan in plans:
                    self.assertEqual(plan['action_type'], 'NEW_PENDING_LIMIT')
                    self.assertGreaterEqual(plan['score'], 65)
                self.assertEqual(direction.active_structure(snap, cfg.tick, engine)['side'], side)

    def test_weak_break_and_flat_value_do_not_override(self):
        snap, cfg, _ = fixture()
        snap.candles['15m'][-1] = replace(snap.candles['15m'][-1], open=74.49, high=74.53,
                                                low=74.47, close=74.52)
        with patch.object(direction, 'session_vwap', return_value={'value':74.50}), \
                patch.object(organic, 'ema20', return_value=74.50):
            self.assertFalse(direction.active_structure(snap, cfg.tick, engine))
            self.assertFalse(organic.formations(snap, cfg.tick, engine))

    def test_raid_engulfing_has_reversal_proof_without_invented_bos(self):
        for side in (1, -1):
            snap, cfg, _ = fixture(side)
            def bar(ts, o, h, l, c):
                return (engine.Candle(ts,o,h,l,c,100.) if side == 1 else
                        engine.Candle(ts,150-o,150-l,150-h,150-c,100.))
            rows = [bar(c.ts,74.56,74.63,74.37,74.58) for c in snap.candles['15m']]
            rows[-2] = bar(NOW-2*STEP,74.56,74.62,74.38,74.43)
            rows[-1] = bar(NOW-STEP,74.42,74.65,74.27,74.60)
            snap.candles['15m'] = rows
            snap.price = rows[-1].close
            snap.bid, snap.ask = snap.price-.01, snap.price+.01
            formations = organic.formations(snap,cfg.tick,engine)
            setup = next(s for s in formations if s['family'] == scanner.ORGANIC_REVERSAL)
            self.assertFalse(setup['origin_broke_structure'])
            self.assertLessEqual(setup['entry_zone']['sweep']['reference_known_ts'], rows[-1].ts)
            state = engine.new_state(cfg)
            with patch.object(engine, 'full_targets', return_value=target_for(side)):
                result = engine.analyze(snap,cfg,state)
            plan = next((p for p in result['plans'] if p['source_strategy'] == scanner.ORGANIC_REVERSAL),None)
            self.assertIsNotNone(plan,result['rejections'])
            self.assertGreaterEqual(plan['score'], 65)
            self.assertEqual(plan['confluence']['route'],'REVERSAL_SWEEP')
            self.assertFalse(plan['origin_broke_structure'])

    def test_organic_flag_is_not_evidence(self):
        snap, cfg, _ = fixture()
        plan = {'side':1,'organic_confluence':True,'origin_broke_structure':True,
                'm15_momentum_aligned':True,'policy':bzu_policy.POLICY,
                'entry_zone':{'low':74.,'high':74.5,'kind':'FVG'}}
        with patch.object(ict,'session_sweeps',return_value=[]):
            score = quality.confluence(plan,snap,{},cfg,engine)
        self.assertEqual(score['score'],25)
        self.assertFalse(score['accepted'])
        self.assertFalse(ict.entry_trigger(plan,{}))

    def test_5m_can_release_inside_unfinished_15m(self):
        start = NOW
        zone = {'low':98.0,'high':98.3}
        first = engine.Candle(start,100.,100.1,97.9,98.1)
        last = engine.Candle(start+300_000,98.08,100.22,98.02,100.15)
        snap = engine.Snapshot(start+300_000,98.1,{'5m':[first,last]})
        self.assertTrue(adverse.approach(snap,1.,1,zone,.01)['blocked'])
        snap = replace(snap,now=start+600_000,price=100.15)
        verdict = adverse.approach(snap,1.,1,zone,.01)
        self.assertFalse(verdict['blocked'])
        self.assertEqual(verdict['confirmation_timeframe'],'5m')
        self.assertEqual(verdict['confirmation_known_ts'],snap.now)


class ExitTests(unittest.TestCase):
    def plan(self, side=1):
        snap,cfg,ctx = fixture(side)
        anchor = anchor_for(snap,cfg,ctx)
        with patch.object(engine,'full_targets',return_value=target_for(side)):
            plan, reason = engine.full_plan(anchor,ctx,snap,cfg,{})
        self.assertIsNotNone(plan, reason)
        return snap,cfg,plan

    def test_htf_magnet_front_runs_level_without_requiring_a_runner(self):
        for side in (1, -1):
            snap,cfg,ctx = fixture(side)
            anchor = anchor_for(snap,cfg,ctx)
            entry, risk = (74.5 if side == 1 else 75.5), .40
            magnet = {'level':entry + side*.54,'timeframe':'4H','hard_barrier':True}
            with patch.object(engine,'full_targets',return_value=[magnet]):
                geometry, reason = bzu_policy.protected_targets(
                    ctx,snap,anchor,cfg,engine,entry,entry-side*risk,side,.3,False)
            self.assertIsNotNone(geometry, reason)
            self.assertAlmostEqual(geometry['tp1'],magnet['level'] - side*.03)
            self.assertEqual(geometry['tp2'],geometry['tp1'])

    def test_no_early_be_and_tp1_arms_two_cent_stop_for_runner(self):
        for side in (1, -1):
            snap,cfg,plan = self.plan(side)
            entry,risk = plan['entry'],plan['risk']
            def bar(ts, favorable, close):
                a,b=sorted((entry + side*favorable,entry))
                return engine.Candle(ts,entry,b,a,entry+side*close)
            p = bzu_policy.first_fill(plan,bar(NOW,0,0),engine,cfg,60_000)
            book = engine.new_state(cfg)
            book['active'] = p
            engine.manage_bar(book,bar(NOW+60_000,1.16*risk,.9*risk),cfg,step=60_000,snapshot=snap)
            self.assertNotIn('pending_stop',p)
            engine.manage_bar(book,bar(NOW+120_000,side*(p['tp1']-entry),1.2*risk),cfg,step=60_000)
            self.assertTrue(p['tp1_hit'])
            self.assertAlmostEqual(p['pending_stop']['price'],entry+side*.02)
            self.assertAlmostEqual(p['remaining'],.5)
            self.assertEqual(p['pending_stop']['effective_ts'],NOW+180_000)

    def test_structural_trail_only_after_confirmed_post_entry_swing(self):
        snap,cfg,plan = self.plan()
        entry,risk = plan['entry'],plan['risk']
        p = bzu_policy.first_fill(plan,engine.Candle(NOW,entry,entry,entry,entry),engine,cfg,60_000)
        book = engine.new_state(cfg)
        book['active'] = p
        rows = [engine.Candle(NOW+i*STEP,entry+.17,entry+.20,entry+low,entry+.17)
                for i,low in enumerate((.12,.08,.14))]
        s = replace(snap,now=NOW+3*STEP,candles={'15m':rows})
        end = s.now
        first = engine.Candle(end-120_000,entry+.17,entry+1.2*risk,entry+.16,entry+.18)
        engine.manage_bar(book,first,cfg,step=60_000,snapshot=s)
        self.assertNotIn('pending_stop',p)
        engine.manage_bar(book,replace(first,ts=end-60_000),cfg,step=60_000,snapshot=s)
        self.assertAlmostEqual(p['pending_stop']['price'],entry+.08-cfg.tick)
        self.assertEqual(p['pending_stop']['effective_ts'],end)

    def test_v24_plan_keeps_saved_exit_contract(self):
        snap,cfg,plan = self.plan()
        plan.update(policy=bzu_policy.MOMENTUM_POLICY,exit_policy='BE_1R_ROUND_TP1_50_TP2_50',
                    breakeven_trigger_r=1.)
        entry,risk = plan['entry'],plan['risk']
        c = engine.Candle(NOW,entry,entry,entry,entry)
        p = bzu_policy.first_fill(plan,c,engine,cfg,60_000)
        book = engine.new_state(cfg)
        book['active'] = p
        engine.manage_bar(book,replace(c,ts=NOW+60_000,high=entry+1.04*risk),cfg,step=60_000)
        self.assertEqual(p['exit_policy'],'BE_1R_ROUND_TP1_50_TP2_50')
        self.assertTrue(p['breakeven_armed'])


class IntegrationTests(unittest.TestCase):
    def test_dynamic_step_requires_new_structure_on_same_side(self):
        for side in (1,-1):
            snap,cfg,_ = fixture(side)
            old = {'id':'old','event_key':'old','side':side,'placed_ts':NOW-STEP,
                   'entry':snap.price-side*.80,'expires_ts':NOW+STEP,'score':80.}
            new = {'id':'new','event_key':'new','side':side,'action_type':'NEW_PENDING_LIMIT',
                   'entry':snap.price-side*.2,'quality':{'accepted':True},'score':72.,
                   'entry_zone':{'kind':'FVG','known_ts':NOW},'zone_origin':{'confirmed':True,'break_known_ts':NOW}}
            audit = arbitration.decision([old],[new],snap,cfg,direction,engine=engine)
            self.assertEqual(audit['reason'],'DYNAMIC_ORDER_STEP')
            self.assertGreater(audit['distance_atr'],1.3)
            new['entry_zone']['known_ts'] = old['placed_ts']
            self.assertNotEqual(arbitration.decision([old],[new],snap,cfg,direction,engine=engine)['decision'],
                                'REQUEST_REPLACEMENT')

    def test_public_snapshot_and_workflow(self):
        snap,cfg,_ = fixture()
        state = engine.new_state(cfg)
        state['private_token'] = 'test sentinel'
        with patch.object(engine,'full_targets',return_value=target_for(1)):
            result = engine.run_cycle(state,snap,cfg)
        public = live_terminal.build(state,snap,result,cfg,engine)
        self.assertEqual(public['status'],'READY')
        self.assertIn('BREAKER_FLIP_15M', {z['kind'] for z in public['zones']})
        self.assertGreaterEqual(len(public['orders']),1)
        self.assertNotIn('test sentinel',str(public))
        self.assertTrue(public['candles']['5m'])
        frozen = live_terminal.build(state,snap,{'action':'DATA_HOLD'},cfg,engine)
        self.assertEqual(frozen['status'],'DATA_HOLD')
        workflow = Path('.github/workflows/main.yml').read_text()
        self.assertEqual(workflow,Path('WORKFLOW_main.yml').read_text())
        self.assertIn("cron: '*/15 * * * *'",workflow)
        self.assertIn(engine.VERSION,workflow)
        self.assertEqual(core.BOT_VERSION,engine.VERSION)
        self.assertEqual(len(core.DETECTORS),24)
        self.assertIn('actions/deploy-pages@v4',workflow)


if __name__ == '__main__':
    unittest.main()
