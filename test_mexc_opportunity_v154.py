import copy,math,unittest
import mexc_multi_scanner as ms
from mexc_opportunity import MODEL_SCHEMA, estimate, shadow_step, register, rates, cost_r, funding_accrual, break_memory, memory_retest, beta_relative
from mexc_lifecycle import advance
from mexc_geometry import POLICY
from test_mexc_direction_v153 import fixture
from unittest.mock import patch
T=1800000000000

def bar(t,o,h,l,c): return dict(t=t//1000,o=o,h=h,l=l,c=c)
def active(side=1): return dict(side=side,entry=100.,risk=1.,stop=100-side,tp=100+2.5*side,tick=.01,opened_ts=T,processed_until_ts=T,phase='OPEN',geometry_policy=POLICY,tp1_enabled=True,tp1=100+side,tp1_r=1.,partial_pct=.3,idea_review_enabled=True,model_schema=MODEL_SCHEMA)
def item(rows,price,t):return dict(candles_1m=rows,tick=.01,bid=price,ask=price,price=price,quote_ts=t,quote_valid=True)
def candidate(side=1):
    return dict(symbol='LINK_USDT',family='MOMENTUM',regime='LOCAL_UP',side=side,order_type='MARKET',entry=100.,stop=100-side,tp=100+2*side,risk=1.,tick=.01,valid=True,signal_id='sig',signal_event_key='evt',signal_known_ts=T,entry_fee_rate=.0002,exit_fee_rate=.0002,entry_slip_rate=.0001,exit_slip_rate=.0001,rejection_codes=[])

class OpportunityTests(unittest.TestCase):
    def test_early_meta_no_future_and_direction(self):
        c=candidate();r={**c,'model_schema':MODEL_SCHEMA,'source':'PAPER','quality':'OBSERVED_M1','net_r':2.,'opened_ts':T-120000,'closed_ts':T-60000,'trade_id':'a'}
        result=estimate([r,r,{**r,'closed_ts':T+1}],c,T)
        self.assertEqual(result['total_n'],1);self.assertAlmostEqual(result['expected_r_estimate'],2/9,places=3);self.assertFalse(result['validated'])
        self.assertEqual(estimate([r],{**c,'side':-1},T)['total_n'],0)
        self.assertEqual(estimate([{**r,'model_schema':'OKX'}],c,T)['total_n'],0)
    def test_shadow_separate_and_no_retroactive_fill(self):
        c=candidate();m={}; x={'symbol':c['symbol'],'analysis_fresh':True,'candidate_diagnostics':[c],'candles_1m':[bar(T-60000,100,103,99.5,100)]}
        shadow_step(m,[x],T);s=register(m,[x],T);self.assertEqual(s['pending'],1)
        register(m,[x],T);self.assertEqual(len(m['opportunity_ledger']['pending']),1)
        shadow_step(m,[{**x,'candles_1m':[bar(T-60000,100,103,99.5,100)]}],T+1);self.assertEqual(len(m['opportunity_ledger']['resolved']),0)
        shadow_step(m,[{**x,'candles_1m':[bar(T,100,102.1,99.9,102)]}],T+60000)
        r=m['opportunity_ledger']['resolved'][0];self.assertEqual(r['reason'],'TP');self.assertLess(r['net_r'],2.);self.assertEqual(r['quality'],'SHADOW_M1_MODEL');self.assertNotIn('cm_history',m)
    def test_shadow_gap_excluded(self):
        c=candidate();m={};x={'symbol':c['symbol'],'analysis_fresh':True,'candidate_diagnostics':[c]};shadow_step(m,[x],T);register(m,[x],T)
        shadow_step(m,[{**x,'candles_1m':[bar(T+60000,100,102.1,99.9,102)]}],T+120000)
        r=m['opportunity_ledger']['resolved'][0];self.assertEqual(r['quality'],'SHADOW_DATA_GAP');self.assertEqual(estimate([r],c,T+180000)['total_n'],0)
    def test_shadow_invalidated_not_simulated(self):
        c={**candidate(),'rejection_codes':['INVALIDATED_AFTER_TRIGGER']};m={};x={'symbol':c['symbol'],'analysis_fresh':True,'candidate_diagnostics':[c]};shadow_step(m,[x],T);s=register(m,[x],T);self.assertEqual(s['pending'],0);self.assertEqual(s['observations'],1)
    def test_tp1_quote_never_uses_old_wick(self):
        for side in (-1,1):
            a=active(side);q=T+30000
            self.assertIsNone(advance(a,item([],100+side*1.1,q),q,round_price=ms.round_price))
            self.assertTrue(a['tp1_hit']);self.assertEqual(a['processed_until_ts'],q)
            # Earlier wick would hit new BE, but current close is still above BE (LONG) / below BE (SHORT).
            old=bar(T,100,101.2,99.8,100.8) if side==1 else bar(T,100,100.2,98.8,99.2)
            self.assertIsNone(advance(a,item([old],100+side*.8,T+60000),T+60000,round_price=ms.round_price)); self.assertTrue(a['tp1_hit'])
    def test_costs_weight_partial_and_no_free_exit(self):
        with patch.dict('os.environ',{'MEXC_MAKER_FEE_RATE':'0','MEXC_TAKER_FEE_RATE':'.0005','MEXC_SLIPPAGE_RATE':'0'}):
            r=rates('LINK_USDT','LIMIT');self.assertEqual(r['entry_fee_rate'],0);self.assertEqual(r['exit_fee_rate'],.0005)
            a={**active(),**r,'tp1_hit':True,'tp1_fill_price':101.};self.assertAlmostEqual(cost_r(a,102.),.0005*(.3*101+.7*102))
    def test_funding_signed_deduplicated_and_partial(self):
        a=active(1);a['tp1_hit_ts']=T+30000;a['partial_pct']=.3
        x={'funding_events':[{'settleTime':T+60000,'fundingRate':.0005}],'candles_1m':[bar(T,100,101,99.8,100)]}
        r,st=funding_accrual(a,x,T+60000);self.assertAlmostEqual(r,.035);self.assertEqual(funding_accrual(a,x,T+120000)[0],r)
        self.assertIn('MARK_PROXY',st)
    def test_both_sides_and_active_policy(self):
        for side in (-1,1):
            with patch.dict('os.environ',{'BZU_ALLOW_M1_ENTRY':'false'}):
                r=ms.analyze_instrument(**fixture(side,'LINK_USDT'))
            self.assertEqual(r['status'],'READY',r['reason_ua']);self.assertEqual(r['side'],side);self.assertEqual(r['model_schema'],MODEL_SCHEMA);self.assertIn('market_state',r)
            m={};s={'instruments':[r],'defaults':{'deposit_usdt':100,'risk_pct':3,'max_leverage':5}}
            ms.update_dual_paper_slots(s,m,now_ms=r['quote_ts']);a=m['cm_active'];self.assertFalse(a['idea_review_enabled']);self.assertFalse(a['time_stop_enabled']);self.assertFalse(a['tp1_enabled']);self.assertLessEqual(s['portfolio']['used_risk_usd'],3.)
    def test_memory_retest_of_older_break(self):
        rows=[ms.Bar(T+i*300000,100,100.1,99.9,100,100) for i in range(35)]
        rows[23]=ms.Bar(rows[23].ts,100.,101.2,99.99,101.1,200)
        for i in range(24,34):rows[i]=ms.Bar(rows[i].ts,101.,101.2,100.7,101.,100)
        rows[34]=ms.Bar(rows[34].ts,100.1,100.7,100.05,100.6,100)
        now=rows[-1].ts+300000;events=break_memory(rows,[],now,.01);self.assertTrue(any(x['ts']==rows[23].ts for x in events))
        r=memory_retest(rows,events,1,.4,.01,'M5');self.assertTrue(r);self.assertEqual(r[0]['opportunity_origin_ts'],rows[23].ts)

if __name__=='__main__':unittest.main()
