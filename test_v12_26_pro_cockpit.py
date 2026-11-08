"""Public projection contracts: no lost watch state or invented executable plans."""
from dataclasses import replace
import copy
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import full_limit_engine as engine
import live_terminal
from test_v12_24_momentum import fixture, anchor_for
from test_v12_28_quant import target_for, structural_witnesses


class CockpitTests(unittest.TestCase):
    def setUp(self):
        self.snap, self.cfg, _ = fixture()
        self.snap.candles['1H'][-1] = replace(self.snap.candles['1H'][-1], volume=200.)
        self.book = engine.new_state(self.cfg)
        self.zone = {'kind':'MOMENTUM_STAIRCASE_15M','low':74.3,'high':74.5,
                     'timeframe':'15m','known_ts':self.snap.now-900_000}

    def project(self, **analysis):
        with patch.object(engine.policy,'market_regime',return_value={'state':'RANGE_LIQUIDITY_ROTATION'}):
            return live_terminal.build(self.book,self.snap,{'action':'WATCH','analysis':analysis},self.cfg,engine)

    def test_impulse_candidate_survives_empty_orders_and_is_not_a_limit(self):
        row={'setup':'MOMENTUM_STAIRCASE_15M','side':1,'entry':74.5,'score':72,
             'entry_zone':self.zone,'reason':'IMPULSE_CONFIRMATION_REQUIRED',
             'approach':{'blocked':True},'private_key':'PRIVATE_SENTINEL'}
        original=copy.deepcopy(row)
        result=self.project(poi_paths=[row])
        self.assertEqual(result['system_mode'],'SCANNING')
        self.assertEqual(result['orders'],[])
        self.assertEqual(result['reference_levels'][0]['status'],'REFERENCE')
        self.assertEqual(result['reference_levels'][0]['engine_score'],72)
        self.assertEqual(result['reference_levels'][0]['score'],min(result['reference_levels'][0]['score_cap'],sum(c['earned'] for c in result['reference_levels'][0]['score_breakdown'])))
        self.assertEqual(result['reference_levels'][0]['entry_source'],'STRUCTURAL_PREPLAN')
        self.assertNotIn('PRIVATE_SENTINEL',json.dumps(result))
        self.assertIsInstance(result['reference_levels'][0]['stop'],(int,float))
        self.assertEqual(row,original)

    def test_planner_entry_targets_and_signed_distance_for_both_sides(self):
        for side in (1,-1):
            entry=self.snap.price-side*.2
            row={'id':'candidate','side':side,'entry':entry,'stop':entry-side*.3,
                 'tp1':entry+side*.5,'tp2':entry+side*.8,'risk':.3,'score':76,
                 'rating':'A','quality':{'accepted':True,'reason':'ACCEPTED'},'setup':'FVG',
                 'entry_zone':{'kind':'FVG_BULL' if side==1 else 'FVG_BEAR','low':entry-.03,'high':entry+.03,'known_ts':self.snap.now,'timeframe':'1H'}}
            result=self.project(admitted_plans=[structural_witnesses(row, self.cfg.tick)])
            candidate=result['radar']['LONG' if side==1 else 'SHORT']
            self.assertEqual(candidate['entry'],entry)
            self.assertEqual(candidate['tp1'],row['tp1'])
            self.assertAlmostEqual(candidate['distance'],-side*.2)
            self.assertAlmostEqual(candidate['distance_pct'],-side*.2/self.snap.price*100)
            self.assertEqual(candidate['distance_ticks'],round(.2/self.cfg.tick))
            self.assertIn(candidate['status'],('CANDIDATE','REFERENCE'))
            self.assertEqual(candidate['entry_source'],'ENGINE_PLAN')

    def test_unassessed_context_zone_never_gets_a_fake_score_or_trade(self):
        from types import SimpleNamespace
        zone=SimpleNamespace(kind='FVG_BULL',side='LONG',timeframe='15m',low=74.1,
            high=74.3,created_ts=self.snap.now-3*900_000,mitigated=False)
        result=self.project(full_context={'zones':{'fvg':[zone]}})
        z=result['zones'][0]
        self.assertIsInstance(z['score'],int)
        self.assertIsInstance(z['stop'],(int,float))
        self.assertFalse(z['execution_admitted'])
        self.assertEqual(z['entry_source'],'STRUCTURAL_PREPLAN')
        self.assertEqual(z['source_high'],74.3)

    def test_retired_and_future_candidates_do_not_appear(self):
        rows=[{'side':1,'entry':74.5,'entry_zone':self.zone,'reason':r}
              for r in live_terminal.RETIRED]
        future={**self.zone,'known_ts':self.snap.now+1}
        result=self.project(poi_paths=rows)
        self.assertFalse(result['candidates'])
        self.assertFalse(result['zones'])
        result=self.project(poi_paths=[{'side':1,'entry':74.5,'entry_zone':future,'reason':'ACCEPTED'}])
        self.assertFalse(result['zones'])

    def test_pending_and_open_win_over_scanner_and_tp1_contract_is_preserved(self):
        with patch.object(engine,'full_targets',return_value=target_for(1)):
            result=engine.run_cycle(self.book,self.snap,self.cfg)
        pending=live_terminal.build(self.book,self.snap,result,self.cfg,engine)
        self.assertEqual(pending['system_mode'],'PENDING')
        self.assertTrue(pending['orders'])
        p=engine.pending_plans(self.book)[0]
        other={**p,'id':'different-proposal','entry':p['entry']+.01,
               'stop':p['stop']-.20,'score':99,'reason':'ACCEPTED'}
        projected=self.project(candidate_plans=[other])
        related=[c for c in projected['zones'] if c.get('order_id')==p['id']]
        self.assertTrue(related)
        for candidate in related:
            self.assertEqual(candidate['entry'],p['entry'])
            self.assertEqual(candidate['stop'],p['stop'])
            self.assertEqual(candidate['ledger_score'],p['score'])
            self.assertEqual(candidate['score'],min(candidate['score_cap'],sum(c['earned'] for c in candidate['score_breakdown'])))
        p['status']='OPEN';p['tp1_hit']=True;p['remaining']=.5
        p['realized_legs']=[{'target':'TP1','fraction':.5,'ts':self.snap.now-900_000}]
        p['pending_stop']={'price':p['breakeven_price'],'effective_ts':self.snap.now+60_000,
                           'policy':engine.bzu_policy.STOP_POLICY}
        p['be_confirmed_ts']=self.snap.now
        with patch.object(engine,'open_plans',return_value=[p]):
            opened=live_terminal.build(self.book,self.snap,result,self.cfg,engine)
        self.assertEqual(opened['system_mode'],'ACTIVE')
        o=opened['orders'][0]
        self.assertTrue(o['tp1_hit'])
        self.assertTrue(o['tp1_partial'])
        self.assertEqual(o['remaining'],.5)
        self.assertEqual(o['pending_stop_effective_ts'],self.snap.now+60_000)
        held=live_terminal.build(self.book,self.snap,{'action':'DATA_HOLD'},self.cfg,engine)
        self.assertEqual(held['system_mode'],'DATA_HOLD')

    def test_last_closed_event_survives_even_when_no_recent_cancellation(self):
        self.book['trades']=[{'id':'closed','side':-1,'entry':75.2,'closed_ts':self.snap.now-100*60_000,'net_r':2.1}]
        self.book['events']=[{'kind':'CLOSED','order_id':'closed','ts':self.snap.now-100*60_000,
                              'reason':'TAKE_PROFIT','net_r':2.1,'private':'SECRET'}]
        result=self.project()
        self.assertEqual(result['last_event']['kind'],'CLOSED')
        self.assertEqual(result['last_event']['entry'],75.2)
        self.assertEqual(result['last_event']['net_r'],2.1)
        self.assertNotIn('SECRET',json.dumps(result))

    def test_same_zone_proposals_never_mix_entry_and_stop(self):
        a={'setup':'FVG','side':1,'entry':74.5,'stop':74.1,'risk':.4,'tp1':75.1,
           'score':70,'entry_zone':self.zone,'reason':'ACCEPTED'}
        b={'setup':'OTHER','side':1,'entry':74.4,'score':75,'entry_zone':self.zone,
           'reason':'IMPULSE_CONFIRMATION_REQUIRED'}
        result=self.project(poi_paths=[a,b])
        candidates=result['plan_pool']
        self.assertEqual(len(candidates),1)
        z=candidates[0]
        self.assertTrue(all(isinstance(z[k],(int,float)) for k in ('entry','stop','tp1','tp2','score')))
        self.assertAlmostEqual(z['risk'],z['side']*(z['entry']-z['stop']))

    def test_duplicate_paths_are_collapsed_and_payload_is_bounded_per_side(self):
        rows=[{'setup':'FVG','side':side,'entry':self.snap.price-side*(.1+i*.01),
               'score':70,'reason':'ACCEPTED'} for side in (1,-1) for i in range(100)]
        result=self.project(poi_paths=rows+rows)
        self.assertEqual(result['candidates'],[])
        self.assertLessEqual(len(result['reference_levels']),48)
        self.assertIsNone(result['radar']['LONG'])

    def test_impulse_brake_keeps_observed_zone_before_geometry(self):
        for side in (1,-1):
            snap,cfg,ctx=fixture(side)
            anchor=anchor_for(snap,cfg,ctx)
            diagnostics={}
            with patch('bzu_policy.adverse.approach',return_value={'blocked':True}):
                plan,reason=engine.full_plan(anchor,ctx,snap,cfg,{},diagnostics)
            self.assertIsNone(plan)
            self.assertEqual(reason,'IMPULSE_CONFIRMATION_REQUIRED')
            self.assertIn('entry_zone',diagnostics)
            self.assertIn('entry',diagnostics)
            self.assertIn('stop',diagnostics)

    def test_pages_contains_css_and_release_versions_match(self):
        workflow=Path('.github/workflows/main.yml').read_text()
        self.assertIn('cp index.html live_terminal.js live_terminal.css live_state.json',workflow)
        self.assertEqual(workflow,Path('WORKFLOW_main.yml').read_text())
        self.assertIn(engine.VERSION,workflow)
        self.assertIn('live_terminal.css',Path('index.html').read_text())


if __name__=='__main__':
    unittest.main()
