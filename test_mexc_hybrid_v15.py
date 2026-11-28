"""Deterministic regressions for v15. No exchange access or live orders."""
import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import mexc_multi_scanner as ms
from mexc_hybrid import detect, meta_estimate, MODEL_SCHEMA
from mexc_lifecycle import advance
from mexc_worker import serve
from urllib.request import urlopen

T=1_795_700_040_000  # minute aligned


def b(ts,o,h,l,c,v=100): return ms.Bar(ts,o,h,l,c,v)
def packed(ts,o,h,l,c): return dict(t=ts//1000,o=o,h=h,l=l,c=c)
def active(side=1,opened=T,**kw):
    return dict(side=side,entry=100.,risk=1.,stop=100.-side,tp=100.+2*side,tick=.01,opened_ts=opened,processed_until_ts=opened,peak_r=0.,quality='OBSERVED_M1',**kw)
def item(rows,quote=100,ts=T,valid=False):
    return dict(candles_1m=rows,bid=quote,ask=quote,price=quote,quote_ts=ts,quote_valid=valid,tick=.01)


class LifecycleTests(unittest.TestCase):
    def test_repeated_old_candle_cannot_stop_new_be_long(self):
        a=active(); rows=[packed(T,100,101,99.8,100.7)]
        self.assertIsNone(advance(a,item(rows),T+60000,round_price=ms.round_price))
        self.assertEqual(a['stop'],100.05); self.assertEqual(a['processed_until_ts'],T+60000)
        self.assertIsNone(advance(a,item(rows),T+60000,round_price=ms.round_price))
        self.assertEqual(a['stop_effective_ts'],T+60000)
        hit=advance(a,item(rows+[packed(T+60000,100.7,100.9,100.01,100.2)]),T+120000,round_price=ms.round_price)
        self.assertEqual(hit['reason'],'BE_STOP'); self.assertEqual(hit['exit'],100.05)

    def test_repeated_old_candle_cannot_stop_new_be_short(self):
        a=active(-1); rows=[packed(T,100,100.2,99,99.3)]
        self.assertIsNone(advance(a,item(rows),T+60000,round_price=ms.round_price))
        self.assertEqual(a['stop'],99.95)
        self.assertIsNone(advance(a,item(rows),T+60000,round_price=ms.round_price))
        hit=advance(a,item(rows+[packed(T+60000,99.3,100,99.1,99.8)]),T+120000,round_price=ms.round_price)
        self.assertEqual(hit['exit'],99.95)

    def test_open_inside_m1_close_beyond_sl(self):
        a=active(opened=T+30000)
        hit=advance(a,item([packed(T,100,101,98.5,98.5)]),T+60000,round_price=ms.round_price)
        self.assertEqual(hit['reason'],'STOP'); self.assertEqual(hit['exit'],98.5)
        self.assertEqual(hit['quality'],'PARTIAL_ENTRY_BAR')

    def test_preentry_low_not_used(self):
        a=active(opened=T+30000)
        self.assertIsNone(advance(a,item([packed(T,100,100.2,98.8,100.1)]),T+60000,round_price=ms.round_price))
        self.assertEqual(a['quality'],'PARTIAL_ENTRY_BAR')
        self.assertEqual(a['processed_until_ts'],T+60000)

    def test_quote_stop_without_closed_m1(self):
        a=active(opened=T+10000)
        hit=advance(a,item([],98.6,T+30000,True),T+30000,round_price=ms.round_price)
        self.assertEqual(hit['reason'],'STOP'); self.assertEqual(hit['exit'],98.6)

    def test_gap_and_both_barriers(self):
        hit=advance(active(),item([packed(T,95,95.5,94.5,95.2)]),T+60000,round_price=ms.round_price)
        self.assertEqual(hit['exit'],95)
        a=active(); hit=advance(a,item([packed(T,100,102.5,98.5,100.1)]),T+60000,round_price=ms.round_price)
        self.assertEqual(hit['reason'],'STOP'); self.assertTrue(a['ambiguous_ohlc'])

    def test_future_bar_and_future_quote_ignored(self):
        a=active(); result=advance(a,item([packed(T,100,100.1,98,98.5)],98,T+120000,True),T+30000,round_price=ms.round_price)
        self.assertIsNone(result); self.assertEqual(a['processed_until_ts'],T)

    def test_restart_cursor_preserved(self):
        a=active(); r=[packed(T,100,101.5,99.5,101)]
        advance(a,item(r),T+60000,round_price=ms.round_price)
        import json
        a=json.loads(json.dumps(a)); self.assertIsNone(advance(a,item(r),T+120000,round_price=ms.round_price)); self.assertEqual(a['stop'],100.55)

    def test_limit_placement_not_fill_and_expiry(self):
        a=active(phase='PENDING_LIMIT',placed_ts=T,expires_ts=T+120000)
        a['opened_ts']=None
        self.assertIsNone(advance(a,item([packed(T,101,101.4,100.3,101)]),T+60000,round_price=ms.round_price)); self.assertIsNone(a['opened_ts'])
        self.assertEqual(advance(a,item([packed(T+60000,101,101.5,100.4,101)]),T+120000,round_price=ms.round_price)['reason'],'LIMIT_EXPIRED')

    def test_limit_fill_bar_no_optimistic_take(self):
        a=active(phase='PENDING_LIMIT',placed_ts=T); a['opened_ts']=None
        self.assertIsNone(advance(a,item([packed(T,100.5,102.5,99.5,101)]),T+60000,round_price=ms.round_price)); self.assertEqual(a['phase'],'OPEN'); self.assertEqual(a['opened_ts'],T+60000)
        self.assertIsNone(advance(a,item([packed(T,100.5,102.5,99.5,101)]),T+60000,round_price=ms.round_price))

    def test_legacy_stop_migration_does_not_replay(self):
        a=active(); del a['processed_until_ts']; a['stop']=100.55; a['be_armed']=True
        self.assertIsNone(advance(a,item([packed(T,100,101.5,99.8,101)]),T+120000,round_price=ms.round_price,legacy_cursor=T+60000))
        self.assertEqual(a['quality'],'LEGACY_CURSOR_MIGRATION')

    def test_missing_bars_flag_gap(self):
        a=active(); self.assertIsNone(advance(a,item([packed(T+120000,100,100.1,99.9,100)]),T+180000,round_price=ms.round_price)); self.assertTrue(a['data_gap'])


class StrategyTests(unittest.TestCase):
    def test_six_families_and_mirror(self):
        common=[b(T+i*60000,100,100.25,99.75,100) for i in range(30)]
        patterns={
            'LIQUIDITY_SWEEP':[(99.7,100.6,99.3,100.5,180)],
            'TREND_PULLBACK':[(99.9,100.65,99.6,100.6,120)],
            'BREAKOUT_RETEST':[(100,101.0,99.95,100.9,150),(100.25,100.8,100.2,100.7,120)],
            'MOMENTUM':[(100,100.4,99.9,100.25,120),(100.25,100.5,100.2,100.45,120),(100.45,101.4,100.4,101.35,190)],
            'FAILED_BREAKOUT':[(100,100.05,99,99.2,150),(99.2,100.4,99.1,100.3,180)],
            'EXHAUSTION':[(100,100.1,99,99.1,120),(99.1,99.2,97.9,98,150),(98,99.3,97.2,99.25,200)],
        }
        for name,pattern in patterns.items():
            rows=common[:-len(pattern)]+[b(T+(30-len(pattern)+i)*60000,*p) for i,p in enumerate(pattern)]
            for side in (1,-1):
                rr=rows if side==1 else [b(x.ts,200-x.open,200-x.low,200-x.high,200-x.close,x.volume) for x in rows]
                got=detect(rr,side=side,atr=.5,tick=.01,min_rvol=1.1,trend=True,zone=100.,timeframe='M1')
                self.assertIn(name,{g['family'] for g in got},(name,side,got))

    def test_meta_no_okx_transfer_no_future_no_false_probability(self):
        c=dict(family='MOMENTUM',regime='TREND_UP')
        rows=[dict(model_schema='OKX',quality='OBSERVED_M1',trade_id=str(i),opened_ts=T-120000,closed_ts=T-60000,net_r=1.,**c) for i in range(100)]
        self.assertIsNone(meta_estimate(rows,c,T)['win_rate_estimate'])
        rows=[dict(r,model_schema=MODEL_SCHEMA,closed_ts=T+60000) for r in rows]
        self.assertEqual(meta_estimate(rows,c,T)['total_n'],0)
        rows=[dict(r,closed_ts=T-60000) for r in rows]
        result=meta_estimate(rows,c,T); self.assertEqual(result['total_n'],100); self.assertEqual(result['status'],'EMPIRICAL_RANK_ONLY_UNVALIDATED'); self.assertGreater(result['rank_adjustment'],0)

    def test_structural_tick_rounding(self):
        self.assertEqual(ms.round_price(100.24,.25),100.25)
        self.assertEqual(ms.round_price(100.08,.05),100.1)

    def test_real_candidate_admitted_and_ranked(self):
        # Deterministic data aligned on interval boundaries; real detectors, no stub of analyze_instrument.
        def series(step,n,drift):
            end=T//step*step
            return [b(end-(n-i)*step,100+(i-n)*drift,100+(i-n+1)*drift+.03,100+(i-n)*drift-.03,100+(i-n+1)*drift,100) for i in range(n)]
        m1=series(60000,60,.015); m1[-1]=b(m1[-1].ts,99.985,100.16,99.97,100.15,240)
        spec=next(x for x in ms.INSTRUMENTS if x['symbol']=='SOL_USDT')
        kwargs=dict(spec=spec,ticker=dict(lastPrice=100.15,bid1=100.14,ask1=100.16),bars_1m=m1,bars_5m=series(300000,60,.03),bars_15m=series(900000,110,.04),bars_1h=series(3600000,45,.1),bars_4h=series(14400000,35,.2),tick=.01,btc_macro=dict(side=1,state='BULLISH',label_ua='up'),now_ms=T)
        row=ms.analyze_instrument(**kwargs)
        self.assertEqual(row['status'],'READY',row); self.assertEqual(row['trigger_tf'],'M1'); self.assertLess(row['stop'],row['entry']); self.assertIsNone(row['meta']['win_rate_estimate'])
        self.assertIn('BZU MULTI',ms.format_telegram_signal(row,kwargs['btc_macro'],[row]))
        mem={}; state=dict(instruments=[row]); ms.update_dual_paper_slots(state,mem,now_ms=T)
        self.assertTrue(state['cm_slot']['occupied']); self.assertIn('processed_until_ts',state['cm_slot']['active'])
        # Missing ticker and spread must not produce actionable orders.
        for ticker in ({},{'lastPrice':100.15,'bid1':99,'ask1':101}):
            self.assertNotEqual(ms.analyze_instrument(**dict(kwargs,ticker=ticker))['status'],'READY')

    def test_fee_costs_can_turn_gross_win_into_net_loss(self):
        a=dict(active(),symbol='SOL_USDT',label='SOL',cluster_ua='CRYPTO',side_ua='LONG',setup='MOMENTUM_NO_PULLBACK_CONTINUATION',family='MOMENTUM',regime='TREND_UP',trade_id='x',model_schema=ms.MODEL_SCHEMA,sizing={'risk_usd':3},fee_rate=.0005,slippage_rate=.0001,opened_kyiv='now',be_armed=True)
        a['stop']=100.05
        row=dict(item([packed(T,100.3,100.5,100,100.1)]),symbol='SOL_USDT',cluster='CRYPTO',status='WAIT',score=0,rr=0)
        mem={'cm_active':a}; state={'instruments':[row]}
        ms.update_dual_paper_slots(state,mem,now_ms=T+60000)
        self.assertLess(mem['cm_history'][0]['net_r'],0); self.assertEqual(state['cm_slot']['daily']['losses'],1)

    def test_cycle_persists_healthy_and_preserves_timestamp_on_outage(self):
        import json, time, urllib.parse, threading
        from test_mexc_multi_scanner import _make_klines
        symbols=[x['symbol'] for x in ms.INSTRUMENTS]+['BTC_USDT']
        lock=threading.Lock(); running=0; peak=0
        def fetch(url):
            nonlocal running,peak
            if url.endswith('/ticker'):
                return {'data':[dict(symbol=x,lastPrice=104.6,bid1=104.59,ask1=104.61) for x in symbols]}
            if url.endswith('/detail'):
                return {'data':[dict(symbol=x,priceUnit=.01) for x in symbols]}
            with lock: running+=1; peak=max(peak,running)
            time.sleep(.01)
            with lock: running-=1
            q=urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
            step=ms.TF_SECONDS[q['interval'][0]]; end=int(q['end'][0])//step*step
            return _make_klines(end-115*step,step,115,100,.04)
        with tempfile.TemporaryDirectory() as d:
            state=Path(d)/'state.json'; mem=Path(d)/'mem.json'
            first=ms.run_cycle(state,mem,fetcher=fetch)
            self.assertEqual(first['health']['fresh_quotes'],7); self.assertGreater(peak,1)
            self.assertTrue(state.exists() and mem.exists()); self.assertNotIn('candles_1m',first['instruments'][0])
            def broken(_): raise OSError('offline')
            second=ms.run_cycle(state,mem,fetcher=broken)
            self.assertEqual(second['updated_ts'],first['updated_ts']); self.assertEqual(second['health']['status'],'FAILED_DATA')
            self.assertIn('last_attempt_ts',json.loads(state.read_text()))

    def test_worker_serves_no_cache_and_current_state(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); state=root/'multi_scanner_state.json'; state.write_text('{"version":"test"}')
            server=serve(root,state,'127.0.0.1',0)
            try:
                with urlopen(f'http://127.0.0.1:{server.server_port}/multi_scanner_state.json',timeout=3) as res:
                    self.assertIn('no-store',res.headers['Cache-Control']); self.assertEqual(res.read(),b'{"version":"test"}')
            finally: server.shutdown(); server.server_close()

    def test_workflow_separate_and_worker_interval(self):
        main=Path('.github/workflows/main.yml').read_text(); self.assertNotIn('python bot_oneshot.py',main); self.assertNotIn('signal_journal_v6_4.json',main)
        self.assertIn('workflow_dispatch:',main); self.assertIn('mexc_worker.py',Path('compose.yaml').read_text()); self.assertIn('MEXC_SCAN_INTERVAL_SEC: "60"',Path('compose.yaml').read_text())

if __name__=='__main__': unittest.main()
