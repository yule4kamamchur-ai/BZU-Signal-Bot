import json, math, unittest, tempfile
from pathlib import Path
from copy import deepcopy
from unittest.mock import patch
import mexc_portfolio_intelligence as i
import mexc_h1_trend as h
import mexc_range_research as r
import mexc_multi_scanner as sc
from test_mexc_h1_trend import NOW,bar,item,pos,signal_rows
from test_mexc_api_v1511 import mock_good
from mexc_replay_v154 import download

CONTRACT={'contractSize':.1,'volUnit':1,'minVol':1,'maxVol':1000,'maxLeverage':5}

class PortfolioV159(unittest.TestCase):
    def test_float_ghost_budget(self):
        for rb,nb,reason in [(1e-16,500,'BELOW_MIN_RISK_BUDGET'),(.5,1e-15,'BELOW_MIN_NOTIONAL')]:
            for side in (-1,1):
                sig=h.signal(signal_rows(side),NOW)
                p,status=h.open_position('x','ENERGY',sig,sig['close'],NOW,tick=.01,risk_budget=rb,notional_budget=nb,fee=.0002,slip=.0001,contract=CONTRACT,require_contract=True)
                self.assertIsNone(p);self.assertEqual(status,reason)
                e=96 if side==1 else 104
                rs=dict(side=side,signal_ts=NOW,close=e,atr=.5,mean=100,swing=e-side*.5,lower=95,upper=105)
                p,status=r.open_position('x','ENERGY',rs,e,NOW,tick=.01,risk_budget=rb,notional_budget=nb,fee=.0002,slip=.0001,contract=CONTRACT,require_contract=True)
                self.assertIsNone(p);self.assertEqual(status,reason)
    def test_contract_round_down(self):
        p,status=i.size(100,98,1,500,.0003,CONTRACT,True)
        self.assertEqual(status,'OPEN');self.assertEqual(p['contracts'],4);self.assertEqual(p['qty'],.4)
        self.assertLessEqual(p['reserve_usd'],1);self.assertEqual(p['notional'],40)
        self.assertEqual(i.size(100,98,.3,500,.0003,{**CONTRACT,'minVol':2},True)[1],'BELOW_MIN_CONTRACTS')
        self.assertEqual(i.size(100,98,1,500,.0003,None,True)[1],'MISSING_CONTRACT_SPEC')
        self.assertEqual(i.size(100,99.9,1,6,.0003,None,False)[1],'BELOW_MIN_ALLOCATED_RISK')
        self.assertEqual(i.size(100,80,1,500,.0003,None,False)[1],'BELOW_MIN_NOTIONAL')
    def test_metadata_distinct_from_price_tick(self):
        spec={};ticks=sc.fetch_contract_ticks(lambda url:{'data':[{'symbol':'X','priceUnit':.01,**CONTRACT},{'symbol':'Y','priceUnit':.1}]},spec)
        self.assertEqual(ticks,{'X':.01,'Y':.1});self.assertEqual(spec,{'X':CONTRACT})
    def test_no_new_mixed_preserve_legacy(self):
        b={};row=item();row['contract_spec']=CONTRACT;row['require_contract_spec']=True
        out=h.update(b,[row],NOW,profile='MIXED_PEAK_RANGE',allow_new=False);self.assertFalse(b['positions']);self.assertEqual(out['assets'][0]['status'],'RESEARCH_DRAIN_ONLY')
        legacy=pos();legacy['qty']=.012345;legacy['notional']=legacy['qty']*legacy['entry'];b['positions']['UKOIL_USDT']=legacy
        out=h.update(b,[row],NOW,profile='MIXED_PEAK_RANGE',allow_new=False);self.assertEqual(b['positions']['UKOIL_USDT']['qty'],.012345)
    def test_legacy_dust_voided_but_history_preserved(self):
        b={};row=item();h.update(b,[row],NOW)
        dust=pos();dust.pop('execution_schema',None);dust.update(notional=1e-15,reserve_usd=1e-16,qty=1e-17)
        b['positions']['UKOIL_USDT']=dust
        historical={**dust,'net_usd':-1e-16,'net_r':-1.,'closed_ts':NOW-1}
        b['history']=[historical]
        out=h.update(b,[row],NOW,allow_new=False)
        self.assertEqual(out['legacy_dust_voided'],1);self.assertEqual(out['open_count'],0);self.assertEqual(b['history'],[historical]);self.assertEqual(b['voided_numerical_positions'][0]['position'],dust)

    def test_budget_on_real_contract_units(self):
        rows=[item(s['symbol'],s['cluster']) for s in sc.INSTRUMENTS]
        for row in rows: row.update(contract_spec={**CONTRACT,'contractSize':.01,'maxLeverage':2},require_contract_spec=True)
        b={};out=h.update(b,rows,NOW,profile='ADAPTIVE_H1');self.assertGreater(out['open_count'],0)
        self.assertLessEqual(out['reserved_usdt'],3+1e-8);self.assertLessEqual(sum(p['margin_usd'] for p in b['positions'].values()),100+1e-8)
        for p in b['positions'].values():
            self.assertGreaterEqual(p['notional'],5);self.assertGreaterEqual(p['reserve_usd'],.25-1e-9);self.assertAlmostEqual(p['contracts']%1,0)
    def test_partial_quality_side_regime_no_future(self):
        z=dict(execution_schema=i.EXECUTION_SCHEMA,quality='OBSERVED_M1',strategy='TREND_H1',side=1,symbol='X',entry_regime='TREND_UP',opened_ts=NOW-2000,closed_ts=NOW-1000,net_r=2)
        other=[{**z,'side':-1},{**z,'closed_ts':NOW+1},{**z,'execution_schema':'old'},{**z,'quality':'DATA_GAP'}]
        est=i.estimate([z,*other],'X',1,'TREND_UP','TREND_H1',NOW);self.assertAlmostEqual(est['expected_r'],2/9);self.assertEqual(est['samples'],1)
        self.assertLess(i.estimate([z],'Y',1,'TREND_DOWN','TREND_H1',NOW)['expected_r'],est['expected_r'])
        partial={**z,'quality':'PARTIAL_ENTRY_BAR','entry_bar_range_clear':True};self.assertEqual(i.estimate([partial],'X',1,'TREND_UP','TREND_H1',NOW)['effective_n'],.25)
    def test_correlation_signed_exposure(self):
        rows=[];p=100.
        for j in range(49):
            p*=math.exp(.001*((j%5)-2));rows.append(bar(NOW+j*h.HOUR,p,p+.1,p-.1,p))
        self.assertAlmostEqual(i.correlation(rows,rows),1.)
        self.assertAlmostEqual(i.signed_overlap('X',1,{'Y':{'side':1}},{'X':rows,'Y':rows}),1.)
        self.assertEqual(i.signed_overlap('X',1,{'Y':{'side':-1}},{'X':rows,'Y':rows}),0.)
        self.assertIsNone(i.correlation(rows[:5],rows[:5]))
    def test_early_closed_m5_both_sides(self):
        hrs=[bar(NOW-(31-j)*h.HOUR) for j in range(31)]
        for side in (1,-1):
            last=102 if side==1 else 98
            m5=[bar(NOW,100,100.5,99.5,100),bar(NOW+5*h.MINUTE,100,max(100,last)+.1,min(100,last)-.1,last)]
            self.assertIsNone(h.early_signal(hrs,m5,NOW+9*h.MINUTE))
            sig=h.early_signal(hrs,m5,NOW+10*h.MINUTE);self.assertEqual(sig['side'],side);self.assertEqual(sig['route'],'EARLY_M5_H1_CHANNEL')
            future=bar(NOW+10*h.MINUTE,100,150,50,110)
            self.assertEqual(h.early_signal(hrs,[*m5,future],NOW+10*h.MINUTE),sig)
            held=bar(NOW+10*h.MINUTE,last,last+.1,last-.1,last)
            self.assertEqual(h.early_signal(hrs,[*m5,held],NOW+15*h.MINUTE),sig)
            back=bar(NOW+10*h.MINUTE,last,max(100,last)+.1,min(100,last)-.1,100)
            self.assertIsNone(h.early_signal(hrs,[*m5,back],NOW+15*h.MINUTE))
    def test_m15_lock_causal_long_short(self):
        for side in (1,-1):
            p=pos(side);p['range_exit_policy']='M15_LOCK';p['trail_mult']=0.;e=p['entry'];risk=p['risk'];p['processed_until_ts']=NOW+14*h.MINUTE
            c=e+side*.6*risk;fav=e+side*.85*risk;adv=e-side*.25*risk
            hit,_,_=h.advance_bar(p,bar(NOW+14*h.MINUTE,e,max(e,c,fav,adv),min(e,c,fav,adv),c))
            self.assertIsNone(hit);self.assertGreater(side*(p['stop']-e),0);self.assertEqual(p['stop_effective_ts'],NOW+15*h.MINUTE)
            st=p['stop'];hit,_,_=h.advance_bar(p,bar(NOW+15*h.MINUTE,c,max(c,st+side*.1,st-side*.1),min(c,st+side*.1,st-side*.1),st-side*.1))
            self.assertEqual(hit[2],'M15_PROFIT_LOCK')
    def test_live_missing_metadata_not_new_and_preserves_other_books(self):
        row=item();row['require_contract_spec']=True;b={};out=h.update(b,[row],NOW);self.assertFalse(b['positions']);self.assertEqual(out['assets'][0]['status'],'MISSING_CONTRACT_SPEC')
        row['contract_spec']={**CONTRACT,'contractSize':.01};out=h.update(b,[row],NOW);self.assertEqual(out['open_count'],1)
    def test_workflow_exports_candles_and_contract_metadata(self):
        w=Path('.github/workflows/replay_h1_v158_coverage_fix.yml').read_text()
        self.assertIn('mexc_replay_cache/*.csv',w);self.assertIn('mexc_replay_cache/contract_specs.json',w);self.assertIn('--require-contract-specs',w);self.assertIn('default: PEAK_CURRENT',w)
    def test_strict_replay_metadata_and_execution(self):
        import csv, contextlib, io
        from replay_h1_portfolio import replay,CoverageError
        sym='UKOIL_USDT';start=NOW;end=NOW+6*h.MINUTE
        with tempfile.TemporaryDirectory() as tmp,contextlib.redirect_stdout(io.StringIO()):
            p=Path(tmp);(p/'ticks.json').write_text(json.dumps({sym:.01}));manifest=p/'coverage.json'
            with (p/(sym+'.csv')).open('w',newline='') as f:
                w=csv.writer(f);w.writerow(['ts','open','high','low','close','volume'])
                for t in range(start-31*h.HOUR,end,h.MINUTE):
                    px=102 if t>=start-h.MINUTE else 100
                    w.writerow((t,px,px+.1,px-.1,px,1))
            with self.assertRaises(CoverageError): replay(p,start,end,180,symbols=[sym],coverage_out=manifest,require_contract_specs=True)
            self.assertIn(sym,json.loads(manifest.read_text())['missing_contract_specs'])
            (p/'contract_specs.json').write_text(json.dumps({'specs':{sym:{**CONTRACT,'contractSize':.01}}}))
            z=replay(p,start,end,180,symbols=[sym],coverage_out=manifest,require_contract_specs=True)
            self.assertEqual(z['quantity_policy'],'MEXC_CONTRACT_STEP_CURRENT_SNAPSHOT');self.assertGreater(z['final_snapshot']['open_count'],0)
            for q in z['open_positions'].values(): self.assertEqual(q['quantity_policy'],'MEXC_CONTRACT_STEP')

    def test_download_exports_public_spec(self):
        t=NOW; sym='UKOIL_USDT'
        payload={'success':True,'data':{'time':[t//1000],'open':[100],'high':[101],'low':[99],'close':[100],'vol':[1]}}
        def ticks(contract_specs):
            contract_specs[sym]=CONTRACT;return {sym:.01}
        with tempfile.TemporaryDirectory() as tmp,patch.object(sc,'fetch_contract_ticks',side_effect=ticks),patch.object(sc,'_http_json',return_value=payload):
            p=Path(tmp);download(sc,p,t,t+h.MINUTE,symbols=[sym],export_contracts=True)
            self.assertEqual(json.loads((p/'contract_specs.json').read_text())['specs'][sym],CONTRACT)
            self.assertEqual(len((p/(sym+'.csv')).read_text().splitlines()),2)

if __name__=='__main__':unittest.main()
