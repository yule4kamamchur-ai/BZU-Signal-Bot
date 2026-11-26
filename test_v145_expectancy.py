"""v14.4.5: causal expectancy, routing and execution regressions."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
import bzu_execution_rules as rules
import expectancy_model as ev
import full_limit_engine as engine
import okx_public_feed as feed
import position_monitor as monitor
import shadow_ledger as shadow
import signal_quality as quality

NOW=1794000000000
M=60000
CTX=['BREAKOUT_RETEST',1,'LIMIT',1,'BULLISH_TREND','LOWER','LONDON','NORMAL','NORMAL','NORMAL','WITH','EARLY_LIMIT']

def row(i, net=1.5, ctx=CTX, **kw):
    start=(i+1)*ev.DAY
    return dict(id=str(i),decision_ts=start,closed_ts=start+M,net_r=net,
                entry=100.,risk=1.,expectancy_context=list(ctx),
                expectancy_label_schema=ev.LABEL_SCHEMA,**kw)

class ExpectancyTests(TestCase):
    cfg=engine.Config(tick=.01,include_commission=False,slippage_bps=0)

    def snapshot(self, now=NOW, bars=()):
        return engine.Snapshot(now,100.,{'1m':list(bars)},99.99,100.01,True,now,self.cfg.instrument)

    def test_payoff_and_fill_estimated_separately(self):
        rows=[row(i,2. if i%3 else -.5) for i in range(60)]
        rows += [row(i,None,close_reason='LIMIT_TTL') for i in range(60,90)]
        data=ev.labels(rows,100*ev.DAY)
        counts=ev.counts(data)
        self.assertEqual(counts['[]'],[90.,60.,40.,20.,80.,10.])
        p=ev.estimate(counts,{},CTX)
        self.assertGreater(p['expected_net_r'],.7)
        self.assertGreater(p['p_fill'],.5)
        self.assertLess(p['p_fill'],.8)
        self.assertAlmostEqual(p['expected_attempt_r'],p['p_fill']*p['expected_net_r'])
        self.assertTrue(.35<=ev.risk_scale(p)<=1.)
        self.assertTrue(ev.valid_counts(counts))

    def test_episode_dedup_and_no_future_or_censored(self):
        original=row(0,-1.,source='SHADOW_PAPER')
        duplicate={**original,'id':'different','source':'PORTFOLIO','net_r':1.}
        future=row(50)
        gap=row(2,None,close_reason='DATA_GAP')
        ambiguous=row(3,None,close_reason='LIMIT_TTL',ambiguous_ohlc=True)
        valid=row(4,None,close_reason='LIMIT_TTL')
        data=ev.labels([original,duplicate,future,gap,ambiguous,valid],10*ev.DAY)
        self.assertEqual([r['id'] for r in data],['different','4'])
        self.assertEqual(ev.counts(data)['[]'],[2.,1.,1.,0.,1.,0.])

    def test_walk_forward_with_purged_future_and_insufficient_history(self):
        rows=[]
        for i in range(100):
            for side,net in ((1,2.),(-1,-1.)):
                r=row(i,net,ctx=[CTX[0],side,*CTX[2:]])
                r.update(id=f'{i}:{side}',decision_ts=r['decision_ts']+(0 if side==1 else 10*M),closed_ts=r['closed_ts']+(0 if side==1 else 10*M))
                rows.append(r)
        clean=ev.labels(rows,101*ev.DAY)
        result=ev.walk_forward(clean,ev.DAY,101*ev.DAY)
        self.assertTrue(result['validated'],result)
        self.assertLess(result['brier'],result['baseline_brier'])
        self.assertGreaterEqual(result['sized_net_r'],result['constant_risk_net_r'])
        self.assertEqual(ev.walk_forward(clean[:5],ev.DAY,101*ev.DAY)['status'],'INSUFFICIENT_HOLDOUT')

    def test_prior_requires_validation_and_matching_signature(self):
        state=engine.new_state(self.cfg); snap=self.snapshot()
        prior=dict(schema=ev.SCHEMA,label_schema=ev.LABEL_SCHEMA,engine_version=engine.VERSION,
                   instrument=self.cfg.instrument,signature=quality.calibration_signature(self.cfg),
                   data_end_ts=NOW-M,counts={'[]':[20,20,12,8,24,8]},validation={'validated':True})
        with TemporaryDirectory() as d,patch.dict(os.environ,{'META_PRIORS_FILE':str(Path(d)/'prior.json')}):
            path=Path(os.environ['META_PRIORS_FILE'])
            path.write_text(json.dumps(prior))
            self.assertEqual(quality.prepare_sizing(state,snap,self.cfg,engine),'EMPIRICAL_EXPECTANCY_V3_ROUTES')
            self.assertIsNotNone(snap._expectancy_calibration)
            path.write_text(json.dumps({**prior,'validation':{'validated':False}}))
            self.assertEqual(quality.prepare_sizing(state,snap,self.cfg,engine),'UNVALIDATED_EXPECTANCY_PRIORS')
            self.assertIsNone(snap._expectancy_calibration)
            path.write_text(json.dumps({**prior,'signature':'different'}))
            self.assertEqual(quality.prepare_sizing(state,snap,self.cfg,engine),'INCOMPATIBLE_EXPECTANCY_PRIORS')

    def test_no_probability_only_gate(self):
        snap=self.snapshot(NOW//ev.DAY*ev.DAY+10*3600000)
        snap._execution_calibration=({'[]':[0.,100.]},{})
        s=quality.execution_sizing(snap,1,100.,{'side_4h':1,'side_1h':1},wave_ts=None,rvol=None,room_r=.1,setup='BREAKOUT_RETEST')
        self.assertTrue(s['meta_low_probability'])
        self.assertFalse(s['meta_probability_only_gate'])
        self.assertFalse(s['rejection_required'])

    def test_retest_requires_material_improvement_and_witness(self):
        snap=self.snapshot(); regime={'side_1h':1,'side_4h':1}
        sizing={'rejection_required':True}
        trigger={'limit_entry':99.5,'initial_risk':1.,'strict_rejection':True}
        with patch.object(rules.direction,'trend_references',return_value=(None,None)):
            self.assertEqual(rules.retest_reason(snap,1,100.,trigger,regime,1.,.01,sizing=sizing),'CONFIRMATION_BODY_RETEST')
            self.assertIsNone(rules.retest_reason(snap,1,99.55,trigger,regime,1.,.01,sizing=sizing))
            self.assertIsNone(rules.retest_reason(snap,1,100.,{**trigger,'strict_rejection':False},regime,1.,.01,sizing=sizing))
            self.assertIsNone(rules.retest_reason(snap,1,100.,trigger,{'side_4h':-1},1.,.01,sizing=sizing))

    def test_shared_shadow_market_stop_equals_portfolio(self):
        cfg=self.cfg; now=NOW//M*M; snap=self.snapshot(now)
        plan=dict(id='a',event_key='a',setup='BREAKOUT_RETEST',htf_4h=1,side=1,entry=100.,stop=99.,initial_stop=99.,risk=1.,tp=101.5,tp1=101.5,tp2=101.5,order_type='MARKET',action_type='ACTIVE_ENTRY',policy=engine.bzu_policy.POLICY,exit_policy=engine.bzu_policy.FULL_POSITION_EXIT,post_only=False,placed_ts=now,effective_ts=now,expires_ts=now+100*M,entry_legs=[{'price':100.,'fraction':1.,'status':'PENDING'}],meta_context=['BREAKOUT_RETEST',1,'LOWER','LONDON'],expectancy_context=[*CTX[:2],'MARKET',*CTX[3:]],expectancy_label_schema=ev.LABEL_SCHEMA,entry_fee_rate=0.,risk_multiplier=.65,version=engine.VERSION)
        state=engine.new_state(cfg)
        shadow.watch(state,[plan],snap,cfg,engine)
        bar=engine.Candle(now+M,100.,102.,98.,100.)
        portfolio=engine.new_state(cfg)
        portfolio['active']=engine.bzu_policy.first_fill(plan,engine.Candle(now,100.,100.,100.,100.),engine,cfg,M)
        portfolio['active']['last_bar']=now
        engine.manage_bar(portfolio,bar,cfg,step=M,snapshot=self.snapshot(now+2*M,[bar]))
        shadow.advance(state,self.snapshot(now+2*M,[bar]),cfg,engine)
        result,=shadow.ensure(state)['outcomes']; trade,=portfolio['trades']
        self.assertEqual(result['close_reason'],trade['close_reason'])
        self.assertAlmostEqual(result['net_r'],trade['net_r'])
        self.assertLess(result['net_r'],0.)

    def test_adaptive_exit_profiles_opt_in(self):
        p=dict(policy=engine.bzu_policy.POLICY,exit_policy=engine.bzu_policy.FULL_POSITION_EXIT,adaptive_exit_profile='TREND')
        self.assertEqual(monitor.mfe_lock_steps(p,engine),monitor.MFE_LOCK_STEPS)
        self.assertEqual(monitor.mfe_lock_steps({**p,'adaptive_exit_enabled':True},engine),monitor.TREND_MFE_LOCK_STEPS)

    def test_public_ticker_rejects_crossed_foreign_stale_and_old(self):
        inst=self.cfg.instrument
        data=dict(instId=inst,last='100',bidPx='99.99',askPx='100.01',open24h='99',ts=str(NOW),bidSz='3',askSz='4')
        self.assertIsNone(feed.parse_ticker({**data,'bidPx':'101'},inst))
        self.assertIsNone(feed.parse_ticker({**data,'instId':'BTC-USDT'},inst))
        ticker=feed.PublicTicker(inst)
        def packet(r): return json.dumps({'arg':{'channel':'tickers'},'data':[r]})
        with patch.object(feed.time,'time',return_value=NOW/1000):
            ticker.ingest(packet(data))
            ticker.ingest(packet({**data,'ts':str(NOW-1),'last':'99'}))
        self.assertEqual(ticker.get(NOW)['price'],100.)
        self.assertEqual(ticker.get(NOW)['bid_size'],3.)
        self.assertIsNone(ticker.get(NOW+5001))
