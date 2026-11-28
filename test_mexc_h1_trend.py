import unittest, tempfile, json
from pathlib import Path
from copy import deepcopy
from unittest.mock import patch
import mexc_h1_trend as h
import mexc_multi_scanner as sc
from test_mexc_api_v1511 import mock_good

NOW=1790000000000//h.HOUR*h.HOUR

def bar(t,o=100,hg=101,l=99,c=100): return {'t':t//1000,'o':o,'h':hg,'l':l,'c':c}
def signal_rows(side=1):
    rows=[bar(NOW-(31-i)*h.HOUR) for i in range(30)]
    rows.append(bar(NOW-h.HOUR,100,103,99,102) if side==1 else bar(NOW-h.HOUR,100,101,97,98));return rows

def item(symbol='UKOIL_USDT',cluster='ENERGY',side=1,now=NOW):
    p=102 if side==1 else 98
    return {'symbol':symbol,'cluster':cluster,'tick':.01,'trend_tick_valid':True,'quote_valid':True,'quote_ts':now,'ask':p+.01,'bid':p-.01,'_trend_h1':signal_rows(side),'candles_1m':[bar(NOW-h.MINUTE,p,p+.05,p-.05,p)]}

def pos(side=1,now=NOW):
    sig=h.signal(signal_rows(side),now);p,r=h.open_position('UKOIL_USDT','ENERGY',sig,sig['close'],now,tick=.01,risk_budget=3,notional_budget=500,fee=.0003,slip=0.);assert r=='OPEN';return p

class H1Trend(unittest.TestCase):
    def test_closed_channel_excludes_trigger_and_future(self):
        q=signal_rows();q.append(bar(NOW,102,180,90,170));s=h.signal(q,NOW);self.assertEqual(s['side'],1);self.assertEqual(s['upper'],101);self.assertEqual(s['signal_ts'],NOW);self.assertEqual(h.signal(signal_rows(-1),NOW)['side'],-1)
    def test_no_breakout(self):
        q=signal_rows();q[-1]=bar(NOW-h.HOUR);self.assertEqual(h.signal(q,NOW)['side'],0)
    def test_stale_chase_invalid_level(self):
        s=h.signal(signal_rows(),NOW);kw=dict(tick=.01,risk_budget=3,notional_budget=500,fee=.0003,slip=0)
        for p,t,r in [(105,NOW,'PRICE_TOO_FAR'),(100,NOW,'BREAKOUT_NO_LONGER_VALID'),(102,NOW+16*h.MINUTE,'STALE_H1_BREAKOUT')]: self.assertEqual(h.open_position('x','ENERGY',s,p,t,**kw)[1],r)
    def test_initial_risk_cap_and_no_tp(self):
        for side in (-1,1):
            p=pos(side);self.assertIsNone(p['tp']);self.assertAlmostEqual(p['reserve_usd'],3);self.assertLessEqual(p['notional'],500);self.assertGreaterEqual(p['risk'],2.5*p['atr_entry']-1e-8);self.assertGreater(p['risk'],0)
    def test_causal_ratchet_long(self):
        p=pos();p['processed_until_ts']=NOW+h.HOUR-h.MINUTE;st=p['stop'];hit,_,_=h.advance_bar(p,bar(NOW+h.HOUR-h.MINUTE,104,120,st+.1,119),2.);self.assertIsNone(hit);self.assertGreater(p['stop'],st);self.assertEqual(p['stop_effective_ts'],NOW+h.HOUR)
        hit,_,_=h.advance_bar(p,bar(NOW+h.HOUR,119,120,110,115));self.assertEqual(hit[2],'ATR_TRAIL')
    def test_causal_ratchet_short(self):
        p=pos(-1);p['processed_until_ts']=NOW+h.HOUR-h.MINUTE;st=p['stop'];hit,_,_=h.advance_bar(p,bar(NOW+h.HOUR-h.MINUTE,96,st-.1,80,81),2.);self.assertIsNone(hit);self.assertLess(p['stop'],st);hit,_,_=h.advance_bar(p,bar(NOW+h.HOUR,81,90,80,84));self.assertEqual(hit[2],'ATR_TRAIL')
    def test_gap_stop_executes_at_open_not_old_stop(self):
        p=pos();s=p['stop'];hit,_,_=h.advance_bar(p,bar(NOW+2*h.MINUTE,s-2,s+1,s-3,s));self.assertAlmostEqual(hit[0],s-2);self.assertEqual(p['quality'],'DATA_GAP')
    def test_partial_entry_does_not_stop_on_past_low(self):
        p=pos();p['processed_until_ts']=NOW+15000;p['opened_ts']=NOW+15000;st=p['stop'];hit,_,_=h.advance_bar(p,bar(NOW,102,104,st-2,103));self.assertIsNone(hit);self.assertEqual(p['quality'],'PARTIAL_ENTRY_BAR')
    def test_partial_entry_close_can_stop(self):
        p=pos();p['processed_until_ts']=NOW+15000;st=p['stop'];hit,_,_=h.advance_bar(p,bar(NOW,102,104,st-2,st-1));self.assertEqual(hit[2],'PARTIAL_BAR_CLOSE_STOP')
    def test_both_fixed_levels_stop_first(self):
        p=pos();p['tp']=p['entry']+2*p['risk'];hit,_,_=h.advance_bar(p,bar(NOW,p['entry'],p['tp']+1,p['stop']-1,p['entry']));self.assertEqual(hit[2],'INITIAL_STOP');self.assertEqual(p['quality'],'AMBIGUOUS_OHLC')
    def test_portfolio_budget_no_duplicate(self):
        rows=[item(s['symbol'],s['cluster']) for s in sc.INSTRUMENTS];book={};a=h.update(book,rows,NOW);self.assertEqual(len(a['assets']),7);self.assertGreaterEqual(a['open_count'],3);self.assertLessEqual(a['reserved_usdt'],3+1e-8);self.assertLessEqual(a['notional_usdt'],500+1e-8);self.assertLessEqual(sum(p['reserve_usd'] for p in book['positions'].values() if p['cluster']=='CRYPTO'),1+1e-8);ids=deepcopy(book['seen']);b=h.update(book,rows,NOW);self.assertEqual(ids,book['seen']);self.assertEqual(a['open_count'],b['open_count'])
    def test_missing_quote_or_tick_no_entry(self):
        b={};row=item();row['trend_tick_valid']=False;self.assertEqual(h.update(b,[row],NOW)['open_count'],0);row['trend_tick_valid']=True;row['quote_ts']=NOW-2*h.MINUTE;self.assertEqual(h.update(b,[row],NOW)['open_count'],0)
    def test_funding_dedup_and_cost(self):
        b={};row=item();h.update(b,[row],NOW);p=b['positions']['UKOIL_USDT'];initial=p['entry'];row=deepcopy(row);row['candles_1m']=[bar(NOW,initial,initial+.1,initial-.1,initial)];row['funding_events']=[{'settleTime':NOW+h.MINUTE,'fundingRate':.001}];row['quote_ts']=NOW+h.MINUTE;h.update(b,[row],NOW+h.MINUTE);f=p['funding_r'];self.assertGreater(f,0);h.update(b,[row],NOW+h.MINUTE);self.assertEqual(p['funding_r'],f)
    def test_run_cycle_separate_book_and_legacy_snapshot(self):
        with tempfile.TemporaryDirectory() as d:
            state,mem=Path(d)/'state.json',Path(d)/'mem.json';r=sc.run_cycle(state,mem,fetcher=mock_good);self.assertEqual(r['h1_trend']['mode'],'PAPER_SEPARATE_ACCOUNT');self.assertEqual(len(r['h1_trend']['assets']),7);self.assertIn('oil_slot',r);self.assertNotIn('_trend_h1',r['instruments'][0]);self.assertIn('h1_trend_book',json.loads(mem.read_text()))
if __name__=='__main__': unittest.main()
