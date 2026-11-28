import contextlib,io,json,math,os,tempfile,unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
import mexc_trend_research as tr
import mexc_h1_trend as h
import mexc_portfolio_intelligence as intel
import mexc_alpha_audit_v160 as alpha
import mexc_multi_scanner as sc
from replay_h1_portfolio import replay,select_window
from test_mexc_h1_trend import NOW,bar,item,signal_rows
from test_mexc_api_v1511 import mock_good


class TrendResearchV160(unittest.TestCase):
    def test_features_no_future_and_both_sides(self):
        for side in (1,-1):
            q=signal_rows(side);s=h.signal(q,NOW);later=bar(NOW,100,200,50,150)
            old=tr.breakout_features(q,s,NOW)
            self.assertEqual(old,tr.breakout_features([*q,later],s,NOW,[later]))
            self.assertGreaterEqual(old['quality_v160'],0);self.assertLessEqual(old['quality_v160'],1)
            self.assertGreaterEqual(tr.evidence_weight('H1_QUALITY',old),.65)
            self.assertGreaterEqual(tr.evidence_weight('H1_HA',old),.8)
            self.assertEqual(tr.evidence_weight('H1_FLEX_PURE',old),1.)
    def test_channel_uses_only_prebreak_context(self):
        rows=[]
        for j in range(60):
            w=2. if j<50 else .2;rows.append(bar(NOW-(61-j)*h.HOUR,100,100+w,100-w,100))
        a=bar(NOW-h.HOUR,100,101,99,100);b=bar(NOW-h.HOUR,100,200,50,150)
        self.assertEqual(tr.channel_period([*rows,a],NOW),20)
        self.assertEqual(tr.channel_period([*rows,a],NOW),tr.channel_period([*rows,b],NOW))
    def test_ha_synthetic_not_execution(self):
        for side in (1,-1):
            q=signal_rows(side);a=h.signal(q,NOW);row=item(side=side);control={};challenger={}
            h.update(control,[row],NOW);h.update(challenger,[row],NOW,profile='H1_HA')
            p=control['positions']['UKOIL_USDT'];z=challenger['positions']['UKOIL_USDT']
            for field in ('entry','initial_stop','risk','atr_entry','trail_mult'):self.assertEqual(p[field],z[field])
            self.assertEqual(a['atr'],z['atr_entry']);self.assertIn('ha_support',z['research_features'])
    def test_flex_idle_budget_and_protect_other_groups(self):
        self.assertEqual(tr.flex_budget(3.,0.,{},'CRYPTO',{'CRYPTO'}),1.)
        self.assertAlmostEqual(tr.flex_budget(3.,.9,{'CRYPTO':.9},'CRYPTO',{'CRYPTO','METALS'}),.6)
        self.assertAlmostEqual(tr.flex_budget(3.,2.8,{'CRYPTO':.9,'METALS':1.2,'ENERGY':.7},'CRYPTO',{'CRYPTO'}),.2)
        self.assertEqual(tr.flex_budget(3.,3.,{},'CRYPTO',{'CRYPTO'}),0.)
    def test_profile_budgets_and_isolation(self):
        rows=[item(s['symbol'],s['cluster']) for s in sc.INSTRUMENTS]
        for row in rows:row['contract_spec']={'contractSize':.001,'volUnit':1,'minVol':1,'maxLeverage':5};row['require_contract_spec']=True
        for profile in tr.PROFILES:
            with self.subTest(profile=profile):
                b={};out=h.update(b,deepcopy(rows),NOW,profile=profile);self.assertGreater(out['open_count'],0)
                self.assertLessEqual(out['reserved_usdt'],3+1e-8);self.assertLessEqual(out['notional_usdt'],500+1e-8)
                for p in b['positions'].values():self.assertGreaterEqual(p['notional'],5);self.assertGreaterEqual(p['reserve_usd'],.25-1e-9);self.assertLessEqual(p['reserve_usd'],1+1e-8)
                caps={}
                for p in b['positions'].values():caps[p['cluster']]=caps.get(p['cluster'],0.)+p['reserve_usd']
                self.assertTrue(all(v<=(1.5 if profile in tr.FLEX_PROFILES else 1)+1e-8 for v in caps.values()))
                with self.assertRaises(ValueError):h.update(b,rows,NOW,profile='PEAK_CURRENT')
    def test_oil_profile_preserves_other_symbol_geometry_and_size(self):
        for side in (-1,1):
            row=item(symbol='XPT_USDT',cluster='METALS',side=side);a={};b={};h.update(a,[row],NOW);h.update(b,[row],NOW,profile='H1_OIL_CONTEXT')
            for k in ('entry','initial_stop','qty','notional','reserve_usd'):self.assertEqual(a['positions']['XPT_USDT'][k],b['positions']['XPT_USDT'][k])
    def test_signed_overlap_known_unknown(self):
        rows=[bar(NOW+j*h.HOUR,100+j,101+j,99+j,100+j) for j in range(49)]
        x=tr.overlap_exposure('X',1,{'Y':{'side':1,'reserve_usd':.5},'Z':{'side':-1,'reserve_usd':.5},'M':{'side':1,'reserve_usd':.5}},{'X':rows,'Y':rows,'Z':rows},intel.correlation,3.)
        self.assertAlmostEqual(x['overlap'],.5);self.assertEqual(x['known_pairs'],2);self.assertEqual(x['unknown_pairs'],1)
    def test_forward_guard(self):
        with self.assertRaisesRegex(ValueError,'Post-development'):replay(Path('/does/not/exist'),alpha.iso_ms('2026-11-08'),alpha.iso_ms('2026-11-28'),forward=True)
        with self.assertRaisesRegex(ValueError,'Post-development'):replay(Path('/does/not/exist'),alpha.iso_ms('2026-11-28'),alpha.iso_ms('2026-11-30'),forward=True)
    def test_alpha_no_prior_bar_and_ambiguity(self):
        d=dict(entry=100.,stop=99.,tp=102.,risk=1.,tick=.1,side=1,decision_ts=NOW,order_type='MARKET',entry_fee_rate=.0002,exit_fee_rate=.0002,entry_slip_rate=.0001,exit_slip_rate=.0001)
        b=[sc.Bar(NOW-h.MINUTE,100,103,98,100,1),sc.Bar(NOW,100,103,98,100,1)]
        z=alpha.label(d,b,1,NOW+h.MINUTE);self.assertEqual(z['quality'],'AMBIGUOUS_OHLC');self.assertEqual(z['exit'],99.)
        z=alpha.label(d,b,0,NOW+h.MINUTE);self.assertEqual(z['quality'],'DATA_GAP');self.assertIsNone(z['net_r'])
    def test_alpha_limit_and_gap_handling(self):
        d=dict(entry=100.,stop=99.,tp=102.,risk=1.,tick=.1,side=1,decision_ts=NOW,order_type='LIMIT',limit_ttl_min=1,entry_fee_rate=.0002,exit_fee_rate=.0002,entry_slip_rate=0.,exit_slip_rate=.0001)
        z=alpha.label(d,[sc.Bar(NOW,101,102.5,100.5,101,1)],0,NOW+h.MINUTE);self.assertFalse(z['filled']);self.assertEqual(z['reason'],'UNFILLED_TARGET');self.assertEqual(z['net_r'],0.)
        z=alpha.label(d,[sc.Bar(NOW,101,102.5,99.5,100,1),sc.Bar(NOW+h.MINUTE,100,102.5,99.5,101,1)],0,NOW+2*h.MINUTE);self.assertTrue(z['filled']);self.assertEqual(z['quality'],'AMBIGUOUS_OHLC')
    def test_all_nine_families_zero_safe(self):
        z=alpha.summarize([],NOW,NOW+h.HOUR,180,0,False);self.assertEqual(set(z['families']),set(alpha.FAMILIES));self.assertTrue(all(v['attempts']==0 and v['win_rate'] is None for v in z['families'].values()))
    def test_live_optional_research_does_not_touch_control(self):
        with tempfile.TemporaryDirectory() as folder:
            a,b=Path(folder)/'state.json',Path(folder)/'memory.json'
            with patch.dict(os.environ,{'BZU_V160_RESEARCH':'false'}):r=sc.run_cycle(a,b,fetcher=mock_good)
            self.assertIn('h1_trend',r);self.assertFalse(set(tr.PROFILES)&set(r['portfolio_research']))
            old=json.loads(b.read_text())
            self.assertFalse(set(tr.LIVE_PROFILES)&set(old))
    def test_workflows_new_profiles_and_exports(self):
        w=Path('.github/workflows/replay_h1_v159_replace_v158_coverage_fix.yml').read_text();a=Path('.github/workflows/research_alpha_v160.yml').read_text()
        self.assertTrue(all(p in w for p in tr.PROFILES));self.assertIn('--forward',w);self.assertIn('H1_TREND_DIAGNOSTICS.json',w);self.assertIn('mexc_replay_cache/*.csv',a);self.assertIn('MEXC_REPLAY_REPORT.json',a)

if __name__=='__main__':unittest.main()
