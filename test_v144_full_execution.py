import unittest
from dataclasses import replace
from unittest.mock import patch

import bzu_execution_rules as rules
import bzu_policy
import full_limit_engine as engine
import oil_risk
import okx_order_execution as okx
import position_monitor as monitor

NOW = 1793844000000
M1 = 60_000
M15 = 900_000


class FullPositionTest(unittest.TestCase):
    def setUp(self):
        self.cfg = engine.Config(tick=.01, slippage_bps=0.)

    def position(self, side=1):
        return {'id':'one','side':side,'entry':100.,'stop':100.-side,
                'initial_stop':100.-side,'risk':1.,'tp':100.+side*1.75,
                'tp1':100.+side*1.75,'tp2':100.+side*1.75,
                'exit_policy':bzu_policy.FULL_POSITION_EXIT,'policy':bzu_policy.POLICY,
                'order_type':'MARKET','status':'OPEN','filled_ts':NOW-7*M15,'expires_ts':NOW+M15,
                'filled_fraction':1.,'remaining':1.,'entry_cost':100.,'risk_budget':1.,
                'entry_fee_rate':0.,'entry_legs':[],'partials':{'TP1':.5,'TP2':.5},
                'realized_legs':[]}

    def test_full_exit_closes_once_even_if_old_partial_metadata_exists(self):
        state = engine.new_state(self.cfg)
        state['active'] = self.position()
        bar=engine.Candle(NOW,101.6,101.80,101.5,101.75)
        engine.manage_bar(state,bar,self.cfg,step=M1)
        self.assertIsNone(state['active'])
        self.assertEqual(len(state['trades']),1)
        self.assertEqual(state['trades'][0]['close_reason'],'TAKE_PROFIT')
        self.assertEqual(state['trades'][0]['final_exit_fraction'],1.)
        self.assertFalse(state['trades'][0]['realized_legs'])

    def test_ratchet_levels_are_symmetric(self):
        for side in (1,-1):
            for peak,lock in monitor.MFE_LOCK_STEPS:
                with self.subTest(side=side,peak=peak):
                    p=self.position(side)
                    p['max_favorable_r']=peak
                    guard=monitor.mfe_protection(p,NOW,100+side*peak,self.cfg,engine)
                    self.assertAlmostEqual(guard['price'],100+side*lock)
                    self.assertFalse(guard['exit_now'])

    def test_shallow_adverse_close_tightens_deep_swing_break_exits(self):
        for side in (1,-1):
            p=self.position(side)
            p['adverse_close_pair']={'known_ts':NOW,'level':100.-.12*side,
                                     'ema20':100.,'session_vwap':100.}
            p['protection_structure_level']=100.-.44*side
            mild=monitor.evaluate_active_trade_lifecycle(p,NOW,100.-.20*side,self.cfg,engine)
            self.assertFalse(mild['should_exit'])
            self.assertAlmostEqual(mild['stop'],100.-.55*side)
            self.assertFalse(monitor.evaluate_active_trade_lifecycle(p,NOW,100.-.41*side,self.cfg,engine)['should_exit'])
            hard=monitor.evaluate_active_trade_lifecycle(p,NOW,100.-.46*side,self.cfg,engine)
            self.assertEqual(hard['exit_reason'],'CLOSED_ADVERSE_STRUCTURE_SHIFT')

    def test_risk_uses_observed_volatility_and_does_not_clip_structure(self):
        profile={'atr_15m':.35,'atr_1h':1.2,'adr_d1':3.,'session_multiplier':1.45,'atr_4h':2.6}
        stop,risk,details=oil_risk.stop_geometry(100.,97.,1,profile,[],.01,engine,timeframe='4H')
        self.assertGreaterEqual(risk,3.12)
        self.assertLessEqual(stop,96.93)
        self.assertIsNone(details['gate'])
        stop,risk,_=oil_risk.stop_geometry(100.,101.,-1,profile,[],.01,engine)
        self.assertEqual((stop,risk),(102.61,2.61))

    def test_full_target_and_post_only_penetration(self):
        target,source,_=rules.full_target([{'level':101.8,'side':-1}],100.,1.,1,.4,.01,engine)
        self.assertAlmostEqual(target,101.74)
        self.assertEqual(source,'BUFFERED_LIQUIDITY_POOL')
        price=self.position()
        price['post_only']=True
        self.assertFalse(engine.paper_limit_touch(price,engine.Candle(NOW,100.1,100.2,100.,100.1),self.cfg))
        self.assertTrue(engine.paper_limit_touch(price,engine.Candle(NOW,100.1,100.2,99.99,100.1),self.cfg))

    def test_opening_requires_closed_sweep_and_true_displacement(self):
        open_ts=NOW//86_400_000*86_400_000+13*3_600_000
        rows=[engine.Candle(open_ts+i*M15,100.,100.4,99.7,100.01) for i in range(-20,0)]
        rows.append(engine.Candle(open_ts,99.90,101.00,99.4,100.90))
        now=open_ts+M15
        snapshot=engine.Snapshot(now,100.9,{'15m':rows},100.89,100.90,True,now,'BZ-USDT-SWAP')
        zone={'known_ts':open_ts-M15,'low':99.45,'high':100.0}
        with patch.object(rules,'_sweep',return_value={'level':99.70,'sweep_ts':now}):
            result=rules.opening_trigger(snapshot,1,zone,100.,1.,self.cfg,engine)
            self.assertEqual(result['pattern'],rules.EARLY_NY)
            stale=replace(snapshot,ticker_ts=now-1)
            self.assertIsNone(rules.opening_trigger(stale,1,zone,100.,1.,self.cfg,engine))

    def test_aligned_extension_and_lag_route_to_passive_limit(self):
        snap=engine.Snapshot(NOW,100.,{},99.99,100.01,True,NOW,'BZ-USDT-SWAP')
        proof={'known_ts':NOW-700_000,'reference_close':99.8}
        with patch.object(rules.direction,'trend_references',return_value=(98.8,None)):
            route=rules.retest_reason(snap,1,100.01,proof,{'side_1h':1,'side_4h':1},1.,.01)
        self.assertEqual(route,'HTF_EXTENDED_RETEST')
        snap.metadata['scan_gap_ms']=720_000
        with patch.object(rules.direction,'trend_references',return_value=(None,None)):
            route=rules.retest_reason(snap,1,100.01,proof,{},.8,.01)
        self.assertEqual(route,'GAP_RETEST')
        zone={'kind':'FVG_PULLBACK','low':99.4,'high':99.8}
        self.assertAlmostEqual(rules.retest_entry(snap,zone,1,99.8,route,.01,engine),99.60)

    def test_m5_recovery_validates_alignment_and_missing_bars(self):
        start=NOW//300_000*300_000
        candles=[engine.Candle(ts,100,101,99,100) for ts in (start,start+300_000)]
        snapshot=engine.Snapshot(start+620_000,100,{'5m':candles})
        snapshot.metadata.update(require_1m=True,lifecycle_fallback_5m=start)
        state={'last_run_ts':start+20_000}
        self.assertEqual(engine.execution_series(snapshot),('5m',300_000))
        self.assertFalse(engine.execution_health(snapshot,state))
        snapshot.candles['5m']=candles[1:]
        self.assertEqual(engine.execution_health(snapshot,state),['LIFECYCLE_HISTORY_GAP'])


class FakeOKX:
    def __init__(self):
        self.posts=[]
        self.position=[]
        self.parent=None
        self.algos=[]
        self.events=[]

    def get(self,path,**query):
        self.events.append((path,query))
        if path.endswith('/order'):
            return [self.parent] if self.parent else []
        if path.endswith('/positions'):
            return self.position
        if path.endswith('/orders-algo-pending'):
            return self.algos if query['ordType']=='oco' else []
        if path.endswith('/orders-pending'):
            return []
        if path.endswith('/config'):
            return [{'posMode':'net_mode'}]
        if path.endswith('/instruments'):
            return [{'ctType':'linear','ctValCcy':'BZ','ctVal':'.01','lotSz':'1','minSz':'1'}]
        if path.endswith('/max-size'):
            return [{'maxBuy':'1000','maxSell':'1000'}]
        raise AssertionError(path)

    def post(self,path,payload,*,expiry=None):
        self.posts.append((path,payload,expiry))
        if path.endswith('/order') and not payload.get('reduceOnly'):
            self.parent={'state':'live','accFillSz':'0'}
        return [{}]


class ExchangeTest(unittest.TestCase):
    def test_order_contains_full_bracket_and_repeated_scan_does_not_duplicate(self):
        plan=FullPositionTest().position()
        plan.update(event_key='stable',expires_ts=NOW+M15)
        cfg=engine.Config(tick=.01)
        st=engine.new_state(cfg)
        snap=engine.Snapshot(NOW,100,{},99.99,100.01,True,NOW,'BZ-USDT-SWAP')
        api=FakeOKX()
        book={}
        sent=[]
        result={'selected_plans':[plan],'events':[]}
        with patch.dict('os.environ',{'OKX_RISK_USDT':'','OKX_ORDER_NOTIONAL_USDT':'500'}):
            okx.synchronize(api,st,book,result,snap,cfg,engine,lambda:sent.append('persist'))
            okx.synchronize(api,st,book,result,snap,cfg,engine,lambda:sent.append('persist'))
        self.assertEqual(sent,['persist'])
        self.assertEqual(len(api.posts),1)
        order=api.posts[0][1]
        self.assertEqual((order['ordType'],order['sz']),('market','500'))
        self.assertNotIn('sz',order['attachAlgoOrds'][0])
        self.assertEqual(order['attachAlgoOrds'][0]['tpTriggerPx'],'101.75')
        self.assertEqual(order['attachAlgoOrds'][0]['slTriggerPx'],'99')

    def test_exchange_amends_existing_protection_without_cancelling_it(self):
        plan=FullPositionTest().position()
        cfg=engine.Config(tick=.01,slippage_bps=0.)
        st=engine.new_state(cfg)
        snap=engine.Snapshot(NOW,101.4,{},101.39,101.41,True,NOW,'BZ-USDT-SWAP')
        api=FakeOKX()
        api.parent={'state':'filled','accFillSz':'2','avgPx':'100','fillTime':str(NOW-M1)}
        api.position=[{'pos':'2','posSide':'net','avgPx':'100'}]
        api.algos=[{'algoClOrdId':'p','algoId':'algo','sz':'2','slTriggerPx':'99','tpTriggerPx':'101.75'}]
        book={'order':{'plan':plan,'client_id':'parent','algo_client_id':'p','fallback_algo_id':'f','margin':'cross'}}
        okx.synchronize(api,st,book,{'events':[]},snap,cfg,engine,lambda:None)
        path,payload,_=api.posts[0]
        self.assertEqual(path,'/api/v5/trade/amend-algos')
        self.assertEqual(payload['newSlTriggerPx'],'100.55')
        self.assertFalse(payload['cxlOnFail'])
