"""v15.3 tests: synthetic causal scenarios; not a performance backtest."""
import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch
import mexc_multi_scanner as ms
from mexc_direction import context, closed_prefix, relative_strength, trigger_age_limit, signal_event_key
from mexc_hybrid import MODEL_SCHEMA, meta_estimate

T=1795694400000  # aligned to M1/M5/M15/H1/H4 boundaries


def bar(t,o,h,l,c,v=100): return ms.Bar(t,o,h,l,c,v)


def series(step,n,drift,base=100,width=.08):
    return [bar(T-(n-i)*step,base+(i-n)*drift,base+(i-n+1)*drift+width,base+(i-n)*drift-width,base+(i-n+1)*drift) for i in range(n)]


def mirror(rows):
    return [bar(b.ts,200-b.open,200-b.low,200-b.high,200-b.close,b.volume) for b in rows]


def fixture(side=-1,symbol='LINK_USDT'):
    m5=series(300000,60,.02)
    pat=[(99.94,100.01,99.88,99.93,100),(99.93,99.98,99.70,99.74,150),(99.74,99.78,99.37,99.39,180),(99.39,99.48,99.22,99.26,150),(99.26,99.34,99.20,99.28,100)]
    for j,p in enumerate(pat): m5[-len(pat)+j]=bar(m5[-len(pat)+j].ts,*p)
    m1=series(60000,60,0,base=99.28,width=.015)
    # Five subsequent M1 bars reproduce the last M5 OHLC without invalidating the older thesis.
    post=[(99.26,99.31,99.25,99.30),(99.30,99.34,99.29,99.32),(99.32,99.33,99.20,99.23),(99.23,99.28,99.22,99.26),(99.26,99.30,99.25,99.28)]
    for j,p in enumerate(post): m1[-5+j]=bar(m1[-5+j].ts,*p,20)
    kw=dict(spec=next(s for s in ms.INSTRUMENTS if s['symbol']==symbol),bars_5m=m5,bars_1m=m1,bars_15m=series(900000,110,.035),bars_1h=series(3600000,50,.06),bars_4h=series(14400000,35,.10),btc_bars_15m=series(900000,110,.08),ticker={'lastPrice':99.28,'bid1':99.27,'ask1':99.29},tick=.01,btc_macro={'state':'BULLISH','side':1},now_ms=T)
    if side==1:
        for k in ('bars_5m','bars_1m','bars_15m','bars_1h','bars_4h','btc_bars_15m'): kw[k]=mirror(kw[k])
        kw['ticker']={'lastPrice':100.72,'bid1':100.71,'ask1':100.73}
        kw['btc_macro']={'state':'BEARISH','side':-1}
    return kw


class DirectionTests(unittest.TestCase):
    def setUp(self):
        self.env=patch.dict('os.environ',{'BZU_ALLOW_M1_ENTRY':'false','BZU_PORTFOLIO_RISK_PCT':'3'})
        self.env.start(); self.addCleanup(self.env.stop)

    def test_previous_m5_mirrored_with_opposing_htf_and_btc(self):
        for side in (-1,1):
            for sym in ('LINK_USDT','XPT_USDT'):
                with self.subTest(side=side,symbol=sym):
                    r=ms.analyze_instrument(**fixture(side,sym))
                    self.assertEqual(r['status'],'READY',r.get('reason_ua'))
                    self.assertEqual(r['side'],side)
                    self.assertEqual(r['trigger_offset'],1)
                    self.assertEqual(r['signal_age_sec'],300)
                    self.assertEqual(r['regime'],'TRANSITION_DOWN' if side==-1 else 'TRANSITION_UP')
                    names={x['family'] for x in r['candidate_diagnostics'] if x['side']==side}
                    self.assertTrue({'EARLY_STRUCTURE_SHIFT','COUNTERTREND_WAVE','STRUCTURE_CONTINUATION'}<=names,names)
                    self.assertEqual(len(r['direction_diagnostics'][str(-side)]['windows_checked']),2)
                    if sym=='LINK_USDT':
                        self.assertFalse(r['btc_aligned']);self.assertTrue(r['relative_support'])

    def test_actual_slot_accepts_older_than_five_minutes(self):
        kw=fixture(); kw['now_ms']=T+120000
        kw['bars_1m']+=[bar(T,99.28,99.31,99.25,99.28),bar(T+60000,99.28,99.30,99.26,99.28)]
        r=ms.analyze_instrument(**kw); self.assertEqual(r['status'],'READY',r['reason_ua'])
        self.assertEqual(r['signal_age_sec'],420)
        state={'instruments':[r],'defaults':{'deposit_usdt':100,'risk_pct':3,'max_leverage':5}}; mem={}
        ms.update_dual_paper_slots(state,mem,now_ms=kw['now_ms'])
        self.assertEqual(state['cm_slot']['active']['side'],-1);self.assertEqual(state['cm_slot']['active']['trigger_offset'],1)
        self.assertIn(r['signal_event_key'],mem['consumed_signals'])

    def test_chase_rejected_both_sides(self):
        for side in (-1,1):
            kw=fixture(side); price=kw['ticker']['lastPrice']+side*.6
            kw['ticker']={'lastPrice':price,'bid1':price-.01,'ask1':price+.01}
            r=ms.analyze_instrument(**kw)
            self.assertNotEqual(r['status'],'READY')
            self.assertIn('CHASE_DISTANCE',r['direction_diagnostics'][str(side)]['rejections'])

    def test_invalidation_after_older_bar_is_not_erased_by_recovery(self):
        kw=fixture(); m=kw['bars_1m'];m[-4]=bar(m[-4].ts,99.3,100.10,99.2,99.3)
        r=ms.analyze_instrument(**kw)
        self.assertNotEqual(r['status'],'READY');self.assertIn('INVALIDATED_AFTER_TRIGGER',r['direction_diagnostics']['-1']['rejections'])

    def test_old_bar_missing_minute_is_rejected(self):
        kw=fixture();kw['bars_1m'].pop(-3)
        r=ms.analyze_instrument(**kw)
        self.assertNotEqual(r['status'],'READY');self.assertIn('DELAYED_DATA_GAP',r['direction_diagnostics']['-1']['rejections'])

    def test_wave_already_spent_is_rejected(self):
        kw=fixture();m=kw['bars_1m'];m[-4]=bar(m[-4].ts,99.3,99.4,98.8,99.3)
        r=ms.analyze_instrument(**kw)
        self.assertNotEqual(r['status'],'READY');self.assertIn('DELAYED_WAVE_SPENT',r['direction_diagnostics']['-1']['rejections'])

    def test_same_event_cannot_repeat_through_other_family(self):
        kw=fixture();r=ms.analyze_instrument(**kw)
        kw['consumed_signals']={r['signal_event_key']:T}
        again=ms.analyze_instrument(**kw)
        self.assertNotEqual(again['status'],'READY');self.assertIn('SIGNAL_CONSUMED',again['direction_diagnostics']['-1']['rejections'])
        self.assertEqual(signal_event_key('LINK_USDT',-1,T-240000),signal_event_key('LINK_USDT',-1,T))

    def test_unclosed_bar_ignored(self):
        kw=fixture();r=ms.analyze_instrument(**kw)
        kw['bars_5m'].append(bar(T,99.28,105,99.28,105,10000))
        kw['bars_15m'].append(bar(T,100,115,99,115,10000))
        kw['bars_1m'].append(bar(T,99.28,108,99,108,10000))
        self.assertEqual(r,ms.analyze_instrument(**kw))

    def test_calendar_offset_when_latest_m5_missing(self):
        kw=fixture();kw['bars_5m'].pop();r=ms.analyze_instrument(**kw)
        for d in r['direction_diagnostics'].values():
            self.assertEqual([x['offset'] for x in d['windows_checked'] if x['tf']=='M5'],[1])
        self.assertTrue(all(c['trigger_offset']==1 for c in r.get('candidate_diagnostics',[])))
        self.assertEqual(trigger_age_limit('M5'),600000)

    def test_btc_relative_uses_matching_timestamps_and_no_future(self):
        kw=fixture(); rel=relative_strength(kw['bars_15m'],kw['btc_bars_15m']); self.assertTrue(rel['available']); self.assertEqual(rel['side'],-1)
        self.assertFalse(relative_strength(kw['bars_15m'],kw['btc_bars_15m'][:-1])['available'])
        self.assertFalse(relative_strength(kw['bars_15m'],kw['btc_bars_15m'][:-3]+kw['btc_bars_15m'][-2:])['available'])

    def test_no_trigger_does_not_invent_entry(self):
        kw=fixture();kw['bars_5m']=series(300000,60,0);kw['bars_1m']=series(60000,60,0);kw['ticker']={'lastPrice':100,'bid1':99.99,'ask1':100.01}
        r=ms.analyze_instrument(**kw)
        self.assertNotEqual(r['status'],'READY');self.assertTrue(all(d['detected']==0 for d in r['direction_diagnostics'].values()))

    def test_model_version_and_side_isolation(self):
        rows=[{'trade_id':str(i),'model_schema':'MEXC_HYBRID_15_1','side':1,'quality':'OBSERVED_M1','net_r':1.,'opened_ts':T-120000,'closed_ts':T-60000,'family':'MOMENTUM','regime':'TREND_UP'} for i in range(60)]
        c={'family':'MOMENTUM','regime':'TREND_UP','side':-1}
        self.assertEqual(meta_estimate(rows,c,T)['total_n'],0)
        rows=[dict(x,model_schema=MODEL_SCHEMA) for x in rows]
        self.assertEqual(meta_estimate(rows,c,T)['n'],0)
        self.assertEqual(meta_estimate(rows,c,T)['rank_adjustment'],0)

    def test_separate_metal_and_crypto_share_portfolio_budget(self):
        rows=[]
        for symbol in ('UKOIL_USDT','XPT_USDT','LINK_USDT'):
            r=ms.analyze_instrument(**fixture(-1,symbol)); self.assertEqual(r['status'],'READY',r['reason_ua']);rows.append(r)
        state={'instruments':rows,'defaults':{'deposit_usdt':100,'risk_pct':3,'max_leverage':5}};mem={}
        ms.update_dual_paper_slots(state,mem,now_ms=T)
        self.assertTrue(all(state[k]['occupied'] for k in ('oil_slot','cm_slot','metal_slot')))
        self.assertEqual(mem['metal_active']['symbol'],'XPT_USDT');self.assertEqual(mem['cm_active']['symbol'],'LINK_USDT')
        self.assertLessEqual(state['portfolio']['used_risk_usd'],3.0);self.assertLessEqual(state['portfolio']['used_notional_usd'],500.)

    def test_existing_metal_migration_keeps_trade_and_history(self):
        old={'symbol':'XPT_USDT','entry':1300.,'stop':1290.,'tp':1320.,'risk':10.,'model_schema':'MEXC_HYBRID_15_1','processed_until_ts':T-120000}
        mem={'cm_active':copy.deepcopy(old),'cm_history':[{'symbol':'XPT_USDT','closed_ts':T-10000},{'symbol':'LINK_USDT','closed_ts':T-5000}]}
        ms.migrate_metal_slot(mem);ms.migrate_metal_slot(mem)
        self.assertEqual(mem['metal_active'],old);self.assertIsNone(mem['cm_active']);self.assertEqual(len(mem['metal_history']),1);self.assertEqual(len(mem['cm_history']),1)

    def test_archive_price_wave_recognized_without_inventing_volume(self):
        # This snapshot has prices, but no volume or M1. Only test price-wave recognition.
        path=Path('multi_scanner_state.json')
        state=json.loads(path.read_text())
        if state.get('updated_kyiv')!='27.11.2026 15:10': self.skipTest('Live snapshot changed after installation')
        for sym,hhmm in [('XPT_USDT','14:30'),('LINK_USDT','14:55')]:
            import datetime
            tz=datetime.timezone(datetime.timedelta(hours=2))
            row=next(r for r in state['instruments'] if r['symbol']==sym)
            def unpack(key):return [bar(x['t']*1000,x['o'],x['h'],x['l'],x['c'],0) for x in row[key]]
            hh,mm=map(int,hhmm.split(':')); cut=int(datetime.datetime(2026,11,27,hh,mm,tzinfo=tz).timestamp()*1000)
            ctx=context(closed_prefix(unpack('candles_5m'),300000,cut),closed_prefix(unpack('candles_15m'),900000,cut))
            self.assertEqual(ctx['side'],-1)


if __name__=='__main__':unittest.main()
