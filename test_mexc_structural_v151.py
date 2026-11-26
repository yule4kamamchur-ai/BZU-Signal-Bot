"""Regression tests for structural v15.1; synthetic candles, no live order calls."""
import copy
import json
import time
import tempfile
import unittest
from pathlib import Path
from urllib.request import urlopen
from urllib.error import HTTPError
from unittest.mock import patch
import mexc_multi_scanner as ms
from mexc_geometry import geometry, POLICY, PROFILES
from mexc_lifecycle import advance
from mexc_hybrid import MODEL_SCHEMA, meta_estimate
from mexc_worker import serve
from test_mexc_hybrid_v15 import active, item, packed

T=1_795_700_100_000 # aligned to five-minute boundary

class GeometryTests(unittest.TestCase):
    def test_all_profiles_mirror_and_buffer(self):
        for spec in ms.INSTRUMENTS:
            for side in (1,-1):
                a5=.20; a15=.30; entry=100.; anchor=entry-side*.36
                g=geometry(spec,{'structural_stop':anchor,'family':'TREND_PULLBACK'},entry,side,.01,a5,a15,[],[],.0005,.0001,True)
                with self.subTest(spec=spec['symbol'],side=side):
                    self.assertGreaterEqual(g['risk']+1e-8,PROFILES[spec['symbol']][1]*a15)
                    self.assertGreaterEqual(side*(anchor-g['stop'])+1e-8,g['stop_buffer'])
                    self.assertGreater(side*(g['tp']-entry),0)
                    self.assertTrue(g['valid'])
                    self.assertEqual(g['target_kind'],'VOLATILITY_PROJECTION')
    def test_oil_floor_is_at_least_43_cents(self):
        g=geometry(ms.INSTRUMENTS[0],{'structural_stop':91.57,'family':'LIQUIDITY_SWEEP'},91.72,1,.01,.16857142857142954,.30714285714286,[],[],.0005,.0001,True)
        self.assertGreaterEqual(g['risk']+1e-8,.43)
        self.assertGreater(g['tp'],92.17)
        self.assertTrue(g['valid'])
    def test_near_confirmed_target_not_artificially_extended(self):
        rows=[ms.Bar(T+i*900000,100,hi,99.8,100,1) for i,hi in enumerate([100.1,100.2,100.5,100.2,100.1])]
        g=geometry(ms.INSTRUMENTS[0],{'structural_stop':99.8,'family':'LIQUIDITY_SWEEP'},100,1,.01,.2,.3,rows,[],.0005,.0001,True)
        self.assertEqual(g['target_kind'],'CONFIRMED_LIQUIDITY'); self.assertLess(g['tp'],100.5)
        self.assertFalse(g['valid']); self.assertIn('NEAR_LIQUIDITY',g['geometry_reason'])
    def test_distant_structure_is_not_clamped_inside_swing(self):
        for side in (1,-1):
            anchor=100-side*4
            g=geometry(ms.INSTRUMENTS[0],{'structural_stop':anchor,'family':'MOMENTUM'},100,side,.01,.2,.3,[],[],.0005,.0001,True)
            self.assertFalse(g['valid']); self.assertIn('DISTANT_INVALIDATION',g['geometry_reason'])
            self.assertGreater(side*(anchor-g['stop']),0)
    def test_old_geometry_does_not_train_new_meta(self):
        history=[{'trade_id':str(i),'model_schema':'MEXC_HYBRID_15_0','quality':'OBSERVED_M1','net_r':2,'opened_ts':T-100000,'closed_ts':T-1000,'family':'LIQUIDITY_SWEEP','regime':'RANGE'} for i in range(60)]
        meta=meta_estimate(history,{'family':'LIQUIDITY_SWEEP','regime':'RANGE'},T)
        self.assertEqual(meta['total_n'],0); self.assertEqual(MODEL_SCHEMA,'MEXC_HYBRID_15_1')

class TrailTests(unittest.TestCase):
    def test_no_fixed_early_be(self):
        for side in (1,-1):
            a=active(side,opened=T,geometry_policy=POLICY,trail_start_ts=T)
            a['tp']=100+side*6
            r=packed(T,100,101.5,99.9,101) if side==1 else packed(T,100,100.1,98.5,99)
            self.assertIsNone(advance(a,item([r]),T+60000,round_price=ms.round_price))
            self.assertEqual(a['stop'],100-side)
    def swing_rows(self,side):
        result=[]
        for j,(o,h,l,c) in enumerate([(101.4,102.0,101.1,101.7),(101.5,102.2,101.0,101.7),(101.8,103,101.5,102.8)]):
            if side==-1:o,h,l,c=200-o,200-l,200-h,200-c
            for k in range(5):result.append(packed(T+(j*5+k)*60000,o,h,l,c))
        return result
    def test_structure_only_after_confirmed_m5_and_restart(self):
        for side in (1,-1):
            a=active(side,opened=T,geometry_policy=POLICY,trail_start_ts=T,trail_atr_m5=.4,fee_rate=.0005,slippage_rate=.0001)
            a['tp']=100+side*6; rows=self.swing_rows(side)
            self.assertIsNone(advance(a,item(rows[:14]),T+14*60000,round_price=ms.round_price)); self.assertEqual(a['stop'],100-side)
            a=json.loads(json.dumps(a))
            self.assertIsNone(advance(a,item(rows),T+15*60000,round_price=ms.round_price)); stop=a['stop']
            self.assertGreater(side*(stop-100),.12); self.assertEqual(a['trail_reason'],'CONFIRMED_M5_SWING'); self.assertEqual(a['stop_effective_ts'],T+15*60000)
            self.assertIsNone(advance(a,item(rows),T+15*60000,round_price=ms.round_price)); self.assertEqual(a['stop'],stop)
            ts=T+15*60000; r=packed(ts,stop+side*.2,stop+.3,stop-.3,stop-side*.1)
            self.assertEqual(advance(a,item(rows+[r]),ts+60000,round_price=ms.round_price)['reason'],'BE_STOP')
    def test_missing_minute_cannot_confirm_m5(self):
        a=active(opened=T,geometry_policy=POLICY,trail_start_ts=T); a['tp']=106.
        rows=self.swing_rows(1); rows.pop(7)
        self.assertIsNone(advance(a,item(rows),T+15*60000,round_price=ms.round_price)); self.assertEqual(a['stop'],99.)
    def test_migration_does_not_trail_past(self):
        a=active(opened=T,geometry_policy=POLICY,trail_start_ts=T+15*60000); a['tp']=106.
        self.assertIsNone(advance(a,item(self.swing_rows(1)),T+15*60000,round_price=ms.round_price))
        self.assertEqual(a['stop'],99.); self.assertEqual(a['tp'],106.)

class DataTests(unittest.TestCase):
    def test_aggregation_requires_full_contiguous_bars(self):
        rows=[ms.Bar(T+i*60000,100,101+i,99,100+i/10,2) for i in range(5)]
        self.assertEqual(ms.aggregate_bars(rows[:-1],60,300,T+300000),[])
        self.assertEqual(ms.aggregate_bars(rows,60,300,T+299999),[])
        result=ms.aggregate_bars(rows,60,300,T+300000)
        self.assertEqual(len(result),1); self.assertEqual(result[0].high,105); self.assertEqual(result[0].volume,10)
    def test_empty_kline_retry_recovers(self):
        from test_mexc_multi_scanner import _make_klines
        from collections import Counter
        from urllib.parse import urlparse,parse_qs
        calls=Counter()
        def fetch(url):
            parsed=urlparse(url); path=parsed.path
            syms=['BTC_USDT']+[s['symbol'] for s in ms.INSTRUMENTS]
            if path.endswith('/ticker'):return {'data':[{'symbol':s,'lastPrice':100,'bid1':99.99,'ask1':100.01} for s in syms]}
            if path.endswith('/detail'):return {'data':[{'symbol':s,'priceUnit':.01} for s in syms]}
            sym=path.rsplit('/',1)[-1]; tf=parse_qs(parsed.query)['interval'][0]; key=(sym,tf); calls[key]+=1
            if key==('APT_USDT','Min15') and calls[key]==1:return {'success':True,'data':{}}
            step=ms.TF_SECONDS[tf]; end=T//1000//step*step
            return _make_klines(end-145*step,step,145,100,.001)
        out=ms.run_scan(fetcher=fetch,now_ms=T)
        self.assertEqual(calls['APT_USDT','Min15'],2); self.assertIn('APT_USDT/Min15',out['health']['recovered']); self.assertEqual(out['health']['errors'],0)
    def test_health_endpoint_and_cache(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); state=root/'multi_scanner_state.json'; state.write_text(json.dumps({'updated_ts':int(time.time()*1000),'health':{'status':'OK','expected_interval_sec':180}}))
            server=serve(root,state,'127.0.0.1',0); url=f'http://127.0.0.1:{server.server_port}/healthz'
            try:
                with urlopen(url,timeout=2) as r:self.assertTrue(json.load(r)['ok']); self.assertIn('no-store',r.headers['Cache-Control'])
                state.write_text(json.dumps({'updated_ts':0,'health':{'status':'FAILED_DATA'}}))
                with self.assertRaises(HTTPError) as e:urlopen(url,timeout=2)
                self.assertEqual(e.exception.code,503)
            finally: server.shutdown(); server.server_close()

if __name__=='__main__':unittest.main()
