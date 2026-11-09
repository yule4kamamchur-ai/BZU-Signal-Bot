"""Reproduction and boundary tests for v12.27 public direction integrity."""
from dataclasses import replace
import copy
import json
import os
from unittest.mock import patch
import unittest

import full_limit_engine as engine
import live_terminal
import setup_scanner as scanner
from test_v12_24_momentum import fixture
from test_v12_28_quant import target_for, structural_witnesses


class IntegrityTests(unittest.TestCase):
    def setUp(self):
        self.snap,self.cfg,_=fixture()
        self.snap.candles['1H'][-1] = replace(self.snap.candles['1H'][-1], volume=200.)
        self.snap.price=100.
        self.snap.bid,self.snap.ask=99.99,100.01
        self.book=engine.new_state(self.cfg)

    def row(self,side=1,entry=None,score=75):
        entry=entry if entry is not None else 99.63 if side==1 else 100.37
        return structural_witnesses({'id':'candidate-'+str(side),'side':side,'entry':entry,'stop':entry-side*.3,
                'tp1':entry+side*.6,'tp2':entry+side*.9,'risk':.3,'score':score,'rating':'A',
                'setup':'VALID_PLAN','quality':{'accepted':True,'reason':'ACCEPTED'},
                'entry_zone':{'kind':'FVG_BULL' if side==1 else 'FVG_BEAR','low':entry-.04,
                              'high':entry+.04,'timeframe':'1H','known_ts':self.snap.now}}, self.cfg.tick)

    def project(self,rows,mode='RANGE_LIQUIDITY_ROTATION',admitted=True):
        with patch.object(engine.policy,'market_regime',return_value={'state':mode}),patch.object(engine,'atr',return_value=.5):
            return live_terminal.build(self.book,self.snap,{'action':'WATCH','analysis':{'candidate_plans':rows,'admitted_plans':rows if admitted else []}},self.cfg,engine)

    def test_reported_long_98_35_in_bear_fvg_is_rejected(self):
        damaged=self.row(1,98.35,53)
        damaged.update(stop=100.94,reason='WAIT_CONFIRMED_BREAK_FOR_LIMIT')
        damaged['entry_zone'].update(kind='FVG_BEAR',low=97.75,high=98.96)
        out=self.project([damaged],'BEARISH_ACTIVE')
        self.assertEqual(out['candidates'],[])
        self.assertIsNone(out['radar']['LONG'])
        self.assertFalse(any(z['kind']=='FVG_BEAR' and z['side']==1 for z in out['zones']))
        self.assertTrue(out['candidate_rejections'])

    def test_new_branch_clears_the_previous_side_geometry(self):
        row={'side':-1,'entry':98.35,'stop':100.94,'tp1':97.4,'entry_zone':{'kind':'FVG_BEAR'},'score_status':'CONTEXT'}
        row.update(side=1,reason='WAIT_CONFIRMED_BREAK_FOR_LIMIT')
        scanner.replace_plan_diagnostics(row,{})
        for key in ('entry','stop','tp1','entry_zone'):
            self.assertNotIn(key,row)
        self.assertIn('entry_checks',row)

    def test_direction_locked_types_and_neutral_formations(self):
        for kind,side in scanner.ZONE_DIRECTION.items():
            self.assertTrue(scanner.zone_direction_matches({'kind':kind},side))
            self.assertFalse(scanner.zone_direction_matches({'kind':kind},-side))
        self.assertFalse(scanner.zone_direction_matches({'kind':'FVG_BULL','side':-1},1))
        self.assertFalse(scanner.zone_direction_matches({'kind':'UNKNOWN'},1))
        self.assertTrue(scanner.zone_direction_matches({'kind':'SESSION_SWEEP_ENGULFING_15M'},-1))

    def test_invalid_sl_and_targets_rejected_for_both_sides(self):
        for side in (1,-1):
            for field in ('stop','tp1','tp2'):
                row=self.row(side)
                row[field]=row['entry']+side*.1 if field=='stop' else row['entry']-side*.1
                out=self.project([row])
                self.assertFalse(out['candidates'])
                self.assertTrue(out['candidate_rejections'])

    def test_weak_pattern_and_incomplete_plans_stay_reference_only(self):
        for variant in ('weak','pattern','missing','low_rr'):
            row=self.row()
            if variant=='weak':row['score']=59.99
            if variant=='pattern':row.pop('quality');row.pop('rating');row['reason']='ACCEPTED'
            if variant=='missing':row.pop('tp2')
            if variant=='low_rr':row['tp1']=row['entry']+.39
            out=self.project([row],admitted=variant!='pattern')
            self.assertFalse(out['candidates'],variant)
            self.assertTrue(out['zones'],variant)
            for z in out['zones']:
                self.assertTrue(all(isinstance(z[k],(int,float)) for k in ('entry','stop','tp1','tp2','score')))
                self.assertEqual(z['score'],min(z['score_cap'],sum(c['earned'] for c in z['score_breakdown'])))

    def test_exact_score_and_rr_boundaries_and_input_not_mutated(self):
        row=self.row(score=60);row['tp1']=row['entry']+.3*1.40
        structural_witnesses(row, self.cfg.tick)
        before=copy.deepcopy(row)
        out=self.project([row])
        self.assertEqual(len(out['candidates']),1)
        self.assertGreaterEqual(out['candidates'][0]['rr1'],1.40-1e-8)
        self.assertEqual(row,before)

    def test_bearish_primary_is_short_even_if_long_is_nearer_or_higher_score(self):
        out=self.project([self.row(1,99.5,95),self.row(-1,100.53,75)],'BEARISH_ACTIVE')
        self.assertEqual(out['primary_bias'],-1)
        self.assertIsNone(out['radar']['LONG'])
        self.assertEqual(out['radar']['SHORT']['side'],-1)
        self.assertEqual(out['primary_candidate_id'],out['radar']['SHORT']['id'])
        self.assertTrue(any(r.get('gate_reason')=='BLOCKED_BY_TREND' for r in out['reference_levels']))

    def test_bullish_primary_is_long(self):
        out=self.project([self.row(1),self.row(-1,100.5,95)],'BULLISH_ACTIVE')
        self.assertEqual(out['primary_bias'],1)
        self.assertIsNone(out['radar']['SHORT'])
        self.assertEqual(out['radar']['LONG']['side'],1)

    def test_range_pair_requires_structural_gap(self):
        close=self.project([self.row(1,99.8,75),self.row(-1,100.2,80)])
        self.assertEqual(sum(v is not None for v in close['radar'].values()),1)
        self.assertTrue(close['radar']['SHORT'] or close['radar']['LONG'])
        far=self.project([self.row(1),self.row(-1)])
        self.assertEqual(sum(v is not None for v in far['radar'].values()),2)
        self.assertGreaterEqual(far['radar']['SHORT']['entry']-far['radar']['LONG']['entry'],far['min_structural_spread'])

    def test_inside_or_wrong_side_zone_cannot_be_a_candidate(self):
        for side in (1,-1):
            row=self.row(side,100.)
            out=self.project([row])
            self.assertTrue(out['zones'])
            self.assertFalse(out['candidates'])

    def test_closed_body_through_distal_edge_removes_spent_zone(self):
        row=self.row(-1,100.2);row['entry_zone'].update(low=100.1,high=100.4,known_ts=self.snap.now-1_800_000)
        self.snap.candles['15m']=[engine.Candle(self.snap.now-900_000,100.1,100.5,100.,100.41)]
        # The confirmed close beyond the upper edge invalidates this short zone.
        out=self.project([row])
        self.assertFalse(out['zones'])
        self.assertFalse(out['candidates'])

    def test_formation_bar_never_retroactively_mitigates_a_zone(self):
        row=self.row(-1,100.2);row['entry_zone'].update(low=100.1,high=100.4,known_ts=self.snap.now)
        self.snap.candles['15m']=[engine.Candle(self.snap.now-900_000,100.1,100.4,100.,100.30)]
        out=self.project([row])
        self.assertTrue(out['zones'])

    def test_wide_zone_refined_without_moving_plan_entry_or_stop(self):
        row=self.row(-1,101.)
        row['entry_zone'].update(low=100.8,high=102.)
        out=self.project([row])
        z=out['zones'][0]
        self.assertTrue(z['wide_zone'])
        self.assertAlmostEqual(z['low'],100.8)
        self.assertEqual(z['source_low'],100.8)
        self.assertEqual(z['entry'],row['entry'])
        self.assertEqual(z['stop'],row['stop'])
        self.assertEqual(z['plan_basis'],'ENGINE_GEOMETRY')

    def test_private_fields_not_exported(self):
        row=self.row();row.update(private_token='DO_NOT_EXPORT');row['quality']['private_token']='DO_NOT_EXPORT'
        out=self.project([row])
        self.assertNotIn('DO_NOT_EXPORT',json.dumps(out))

    def test_live_reaction_mode_does_not_place_new_paper_orders(self):
        snap,cfg,_=fixture();book=engine.new_state(cfg)
        with patch.dict(os.environ,{'BZU_ENTRY_MODE':'LIVE_REACTION'}),patch.object(engine,'full_targets',return_value=target_for(1)):
            out=engine.run_cycle(book,snap,cfg)
        self.assertEqual(out['action'],'WATCH_REACTION')
        self.assertTrue(out['analysis']['reaction_plans'])
        self.assertFalse(engine.open_plans(book))
        self.assertFalse(any(e['kind'] in ('PLACED','FILLED') for e in out['events']))
        self.assertEqual(book['entry_mode'],'LIVE_REACTION')

    def test_live_reaction_report_never_claims_an_order_was_placed(self):
        import notification_report
        snap,cfg,_=fixture();book=engine.new_state(cfg)
        with patch.dict(os.environ,{'BZU_ENTRY_MODE':'LIVE_REACTION'}),patch.object(engine,'full_targets',return_value=target_for(1)):
            out=engine.run_cycle(book,snap,cfg)
        report=notification_report.build_checklist(book,snap,out,cfg,engine)
        self.assertIn('LIVE',report)
        self.assertNotIn('НОВИЙ ЛІМІТ',report)
        self.assertFalse(engine.open_plans(book))

    def test_invalid_entry_mode_fails_before_mutating_the_ledger(self):
        before=copy.deepcopy(self.book)
        with patch.dict(os.environ,{'BZU_ENTRY_MODE':'TYPO'}):
            with self.assertRaises(ValueError):engine.run_cycle(self.book,self.snap,self.cfg)
        self.assertEqual(self.book,before)


if __name__=='__main__':unittest.main()
