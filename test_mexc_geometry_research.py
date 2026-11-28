import unittest
from copy import deepcopy
from mexc_geometry_research import update,VARIANTS
from mexc_h1_trend import MINUTE,HOUR

class Geometry(unittest.TestCase):
    def test_paired_isolated_same_entry_and_static_resolution(self):
        t=1800000000000; a={'symbol':'UKOIL_USDT','trade_id':'a','phase':'OPEN','order_type':'MARKET','opened_ts':t,'entry':100.,'side':1,'initial_stop':99.,'tp':102.,'tick':.01,'fee_rate':.0003,'slippage_rate':0.,'family':'MOMENTUM'}; before=deepcopy(a);b={}
        z=update(b,[],[a],t);self.assertEqual(z['pending'],4);self.assertEqual(a,before);self.assertEqual({p['entry'] for p in b['pending'].values()},{100.});update(b,[],[a],t);self.assertEqual(len(b['pending']),4)
        row={'symbol':'UKOIL_USDT','candles_1m':[{'t':t//1000,'o':100.,'h':100.1,'l':98.9,'c':99.2},{'t':t//1000+60,'o':99.2,'h':103.1,'l':99.1,'c':102.9}]};z=update(b,[row],[],t+2*MINUTE);self.assertEqual(z['pending'],0);self.assertEqual(z['complete_observed_pairs'],1);self.assertLess(z['variants']['STATIC_CONTROL']['net_r'],0);self.assertGreater(z['variants']['SL_150']['net_r'],0);self.assertEqual(a,before)
    def test_not_register_limit_or_old_trade(self):
        t=1800000000000;b={};a={'symbol':'x','phase':'OPEN','order_type':'LIMIT','opened_ts':t};self.assertEqual(update(b,[],[a],t)['pending'],0);a.update(order_type='MARKET',opened_ts=t-MINUTE);self.assertEqual(update(b,[],[a],t)['pending'],0)
if __name__=='__main__': unittest.main()
