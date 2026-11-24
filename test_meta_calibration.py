"""Regression checks for causal shadow labels and isolated prior validation."""
import json
import os
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import bzu_policy
import calibrate_meta_model as calibrate
import full_limit_engine as engine
import shadow_ledger as shadow
import signal_quality as quality
import trade_meta_model as model

NOW = 1_794_000_000_000
M1 = shadow.MINUTE


class CalibrationTests(TestCase):
    cfg = engine.Config(tick=.01)
    context = ['PULLBACK_CONTINUATION', -1, 'UPPER', 'LONDON']

    def snapshot(self, now=NOW, rows=()):
        return engine.Snapshot(now,100.,{'1m':rows},99.99,100.01,True,now,self.cfg.instrument)

    def plan(self, level, order_type='MARKET'):
        return {'id':str(level),'event_key':str(level),'setup':self.context[0],'htf_4h':-1,
                'entry':level,'stop':level+1,'risk':1.,'tp':level-1.5,'order_type':order_type,
                'action_type':'ACTIVE_ENTRY' if order_type=='MARKET' else 'NEW_PENDING_LIMIT',
                'policy':bzu_policy.POLICY,'post_only':True,'exit_policy':bzu_policy.FULL_POSITION_EXIT,
                'expires_ts':NOW+120*M1,'placed_ts':NOW,'effective_ts':NOW,'side':-1,
                'meta_context':self.context,'meta_heuristic':.65,'risk_multiplier':.65}

    def test_busy_portfolio_has_independent_deduplicated_shadow_positions(self):
        state = engine.new_state(self.cfg)
        incumbent = self.plan(102.)
        state['active'] = incumbent
        first, second = self.plan(100.), self.plan(101.)
        snap = self.snapshot()
        shadow.watch(state,[first,second,dict(first)],snap,self.cfg,engine)
        self.assertEqual(len(shadow.ensure(state)['pending']),2)
        bar = engine.Candle(NOW,100.,100.1,98.4,99.)
        shadow.advance(state,self.snapshot(NOW+M1,[bar]),self.cfg,engine)
        self.assertEqual(state['active'],incumbent)
        self.assertEqual(state['trades'],[])
        outcomes = shadow.ensure(state)['outcomes']
        self.assertEqual([o['close_reason'] for o in outcomes],['TAKE_PROFIT','TAKE_PROFIT'])
        self.assertEqual(outcomes[0]['net_r'],1.5)
        self.assertEqual(outcomes[0]['mfe_r'],1.5)

    def test_limit_fill_candle_cannot_prove_its_profit_target(self):
        state = engine.new_state(self.cfg)
        candidate = self.plan(100.,'LIMIT')
        candidate['opening_sweep_confirmed'] = True
        shadow.watch(state,[candidate],self.snapshot(),self.cfg,engine)
        hit = engine.Candle(NOW,99.7,100.2,98.4,99.1)
        shadow.advance(state,self.snapshot(NOW+M1,[hit]),self.cfg,engine)
        self.assertFalse(shadow.ensure(state)['outcomes'])
        both = engine.Candle(NOW+M1,99.8,101.1,98.1,99.5)
        shadow.advance(state,self.snapshot(NOW+2*M1,[both]),self.cfg,engine)
        outcome, = shadow.ensure(state)['outcomes']
        self.assertEqual(outcome['close_reason'],'STOP')
        self.assertTrue(outcome['ambiguous_ohlc'])
        self.assertEqual(outcome['net_r'],-1.)

    def test_vertical_barrier_and_gaps_do_not_take_future_candles(self):
        cfg = replace(self.cfg,max_hold_minutes=1)
        state = engine.new_state(cfg)
        shadow.watch(state,[self.plan(100.)],self.snapshot(),cfg,engine)
        before_close = engine.Candle(NOW,100.,100.3,99.4,99.5)
        future = engine.Candle(NOW+M1,100.,101.1,98.,99.)
        shadow.advance(state,self.snapshot(NOW+M1,[before_close,future]),cfg,engine)
        outcome, = shadow.ensure(state)['outcomes']
        self.assertEqual((outcome['close_reason'],outcome['net_r']),('TIME_BARRIER',.5))
        state = engine.new_state(cfg)
        shadow.watch(state,[self.plan(100.)],self.snapshot(),cfg,engine)
        shadow.advance(state,self.snapshot(NOW+2*M1,[future]),cfg,engine)
        outcome, = shadow.ensure(state)['outcomes']
        self.assertEqual(outcome['close_reason'],'DATA_GAP')
        self.assertIsNone(outcome['net_r'])

    def test_runtime_prior_and_decayed_new_labels_set_risk_without_future_data(self):
        snap = self.snapshot()
        state = engine.new_state(self.cfg)
        state['shadow_trades'] = {'outcomes':[
            {'id':'episode','calibration_id':'a','meta_context':self.context,'decision_ts':NOW-4*M1,
             'closed_ts':NOW-2*M1,'net_r':-1.},
            {'id':'future','meta_context':self.context,'decision_ts':NOW-M1,'closed_ts':NOW+M1,'net_r':1.}
        ]}
        state['trades'] = [{'id':'portfolio','calibration_id':'a','meta_context':self.context,
                            'placed_ts':NOW-4*M1,'closed_ts':NOW-M1,'net_r':1.}]
        artifact = {'schema':model.PRIOR_SCHEMA,'label_schema':shadow.LABEL_SCHEMA,'engine_version':engine.VERSION,
                    'instrument':self.cfg.instrument,'signature':quality.calibration_signature(self.cfg),
                    'data_end_ts':NOW-5*M1,'counts':{'[]':[45,60]},'validation':{'validated':True}}
        with TemporaryDirectory() as directory, patch.dict(os.environ,{'META_PRIORS_FILE':str(Path(directory)/'prior.json')}):
            path = Path(os.environ['META_PRIORS_FILE'])
            path.write_text(json.dumps(artifact))
            self.assertEqual(quality.prepare_sizing(state,snap,self.cfg,engine),'EMPIRICAL_BAYES')
            result = quality.execution_sizing(snap,-1,99.99,{'side_4h':-1},
                                              wave_ts=None,rvol=None,room_r=1.5,setup=self.context[0])
            self.assertTrue(result['meta_prob_calibrated'])
            self.assertGreater(result['meta_prob'],.72)
            self.assertEqual(result['risk_multiplier'],1.)
            self.assertAlmostEqual(result['meta_recent_weight'],model.DAILY_DECAY**(M1/model.DAY_MS))
            path.write_text(json.dumps({**artifact,'validation':{'validated':False}}))
            self.assertEqual(quality.prepare_sizing(state,snap,self.cfg,engine),'UNVALIDATED_PRIORS')
            self.assertIsNone(snap._execution_calibration)

    def test_walk_forward_purges_unsettled_labels_and_checks_two_baselines(self):
        rows = []
        for day in range(100):
            for n, side in enumerate((-1,1)):
                start = (day+1)*shadow.DAY + n*6*M1
                rows.append({'id':str(start),'decision_ts':start,'closed_ts':start+2*M1,
                             'meta_context':['TEST',side,'UPPER','ASIA'],'win':int(side==1)})
        # An early decision resolved in the holdout must not train its first fold.
        cross = {**rows[0],'decision_ts':69*shadow.DAY,'closed_ts':72*shadow.DAY}
        rows = sorted([*rows,cross],key=lambda p:p['decision_ts'])
        verdict = model.walk_forward_sizing(rows,shadow.DAY,101*shadow.DAY)
        self.assertEqual(verdict['folds'][0]['train_labels'],140)
        self.assertTrue(verdict['validated'])
        self.assertLess(verdict['brier'],verdict['baseline_brier'])
        self.assertEqual(model.walk_forward_sizing(rows[:5],shadow.DAY,101*shadow.DAY)['status'],
                         'INSUFFICIENT_HOLDOUT')

    def test_download_clamps_requested_history_to_listing_and_uses_300_bar_pages(self):
        listing = 1600*shadow.DAY
        end = listing+170*shadow.DAY
        fetch = lambda url: {'data':[{'instId':self.cfg.instrument,'tickSz':'.01','listTime':str(listing)}]}
        with patch.object(calibrate.research,'fetch_rows',return_value=[{}]) as request:
            data = calibrate.download_history(365,end,15,fetch)
        self.assertEqual(data['start_ts'],listing+calibrate.WARMUP)
        self.assertEqual(request.call_count,len(engine.CANDLE_TF))
        self.assertTrue(all(query.kwargs['page_size']==300 for query in request.call_args_list))
        self.assertEqual(request.call_args_list[0].args[2],listing)

    def test_history_fetch_uses_m1_boundaries_and_completed_bars(self):
        def bar(ts, confirm):
            return [str(ts),'100','101','99','100','1','1','1',str(confirm)]

        result = {'data':[bar(NOW,0),bar(NOW-M1,1),bar(NOW-2*M1,1)]}
        rows = calibrate.research.fetch_rows(self.cfg.instrument,'1m',NOW-2*M1,NOW,
                                             lambda url:result,page_size=300)
        self.assertEqual([row['ts'] for row in rows],[NOW-2*M1,NOW-M1])
