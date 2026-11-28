import unittest,tempfile,json
from pathlib import Path
from copy import deepcopy
import mexc_h1_trend as h
import mexc_range_research as r
import mexc_multi_scanner as sc
from test_mexc_h1_trend import NOW,bar,item,pos
from test_mexc_api_v1511 import mock_good


def range_rows(side=1):
    rows=[]
    for i in range(20):
        p=100+(-1)**i*2
        rows.append(bar(NOW-(21-i)*r.STEP,p,p+.05,p-.05,p))
    last=bar(NOW-r.STEP,95.5,96,94,95.8) if side==1 else bar(NOW-r.STEP,104.5,106,104,104.2)
    rows.append(last)
    return rows

class PortfolioV158(unittest.TestCase):
    def test_range_reclaim_both_sides_no_future(self):
        for side in (1,-1):
            rows=range_rows(side); z=r.signal(rows,NOW);self.assertEqual(z['side'],side)
            rows.append(bar(NOW,100,200,90,150));self.assertEqual(r.signal(rows,NOW),z)
            self.assertIsNone(r.signal(rows[1:],NOW))
    def test_range_missing_minute_gap(self):
        rows=range_rows();rows[10]['t']-=60;self.assertIsNone(r.signal(rows,NOW))
    def test_range_geometry_causal_both_sides(self):
        # Controlled low-ATR reclaim: deliberately different from raw detector fixture.
        for side in (1,-1):
            p0=96 if side==1 else 104
            sig=dict(side=side,signal_ts=NOW,close=p0,atr=.5,mean=100,swing=p0-side*.5,lower=95,upper=105)
            p,status=r.open_position('x','ENERGY',sig,p0,NOW,tick=.01,risk_budget=1,notional_budget=500,fee=.0002,slip=.0001)
            self.assertEqual(status,'OPEN');self.assertEqual(p['strategy'],'RANGE_M15');self.assertEqual(p['trail_mult'],0);self.assertLessEqual(p['reserve_usd'],1+1e-9)
            self.assertAlmostEqual(p['tp'],100);self.assertEqual(r.open_position('x','ENERGY',sig,p0+side*2,NOW,tick=.01,risk_budget=1,notional_budget=500,fee=.0002,slip=.0001)[1],'PRICE_TOO_FAR')
    def test_close_trail_is_not_peak(self):
        a=pos();b=deepcopy(a);b['trail_anchor']='CLOSE';a['processed_until_ts']=b['processed_until_ts']=NOW+h.HOUR-h.MINUTE
        row=bar(NOW+h.HOUR-h.MINUTE,110,120,105,115)
        self.assertIsNone(h.advance_bar(a,row,2)[0]);self.assertIsNone(h.advance_bar(b,row,2)[0]);self.assertAlmostEqual(a['stop']-b['stop'],5);self.assertEqual(a['stop_effective_ts'],NOW+h.HOUR)
    def test_prior_atr_excludes_trigger(self):
        from test_mexc_h1_trend import signal_rows
        rows=signal_rows();self.assertEqual(h.signal(rows,NOW,prior_atr=True)['atr'],h.atr(rows[:-1]));self.assertNotEqual(h.signal(rows,NOW,prior_atr=True)['atr'],h.signal(rows,NOW)['atr'])
    def test_mixed_book_budget_and_isolation(self):
        from unittest.mock import patch
        rows=[item(s['symbol'],s['cluster']) for s in sc.INSTRUMENTS]
        # Only the first row offers an H1 breakout; others offer an M15 reclaim.
        for row in rows[1:]: row['_trend_h1']=[];row['bid']=95.99;row['ask']=96.01
        sig=dict(side=1,signal_ts=NOW,close=96,atr=.5,mean=100,swing=95.5,lower=95,upper=105,efficiency=.1,strength=1.,strategy='RANGE_M15')
        with patch('mexc_range_research.signal',return_value=sig):
            mixed={};out=h.update(mixed,rows,NOW,profile='MIXED_PEAK_RANGE');self.assertLessEqual(out['reserved_usdt'],3+1e-8);self.assertLessEqual(out['notional_usdt'],500+1e-8);self.assertEqual(len(mixed['positions']),len(set(mixed['positions'])));self.assertIn('TREND_H1',{p['strategy'] for p in mixed['positions'].values()});self.assertIn('RANGE_M15',{p['strategy'] for p in mixed['positions'].values()})
        with self.assertRaises(ValueError): h.update(mixed,rows,NOW,profile='CLOSE_PRIOR')
    def test_live_cycle_separate_books_and_drain(self):
        with tempfile.TemporaryDirectory() as folder:
            a,b=Path(folder)/'state.json',Path(folder)/'memory.json';z=sc.run_cycle(a,b,fetcher=mock_good);m=json.loads(b.read_text());self.assertEqual(set(z['portfolio_research']),{'CLOSE_PRIOR','MIXED_PEAK_RANGE','RANGE_ONLY','ADAPTIVE_H1','EARLY_H1_M5','ADAPTIVE_RANGE'});self.assertEqual(m['h1_trend_book']['profile'],'PEAK_CURRENT');self.assertEqual(m['h1_close_book']['profile'],'CLOSE_PRIOR');self.assertEqual(m['mixed_trend_range_book']['profile'],'MIXED_PEAK_RANGE');self.assertTrue(all(v['mode']=='PAPER_SEPARATE_ACCOUNT' for v in z['portfolio_research'].values()));self.assertFalse(z['portfolio_research']['MIXED_PEAK_RANGE']['allow_new'])
if __name__=='__main__':unittest.main()
