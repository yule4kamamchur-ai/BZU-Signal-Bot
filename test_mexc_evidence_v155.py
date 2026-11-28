import copy,unittest
from unittest.mock import patch
import mexc_multi_scanner as ms
from mexc_evidence import active_estimate,exit_policies,tag_experiment
from mexc_opportunity import MODEL_SCHEMA,register,shadow_step,research_summary
from mexc_hybrid import detect
from test_mexc_opportunity_v154 import candidate,bar,T
from test_mexc_direction_v153 import fixture
from mexc_geometry import POLICY

class EvidenceTests(unittest.TestCase):
    def test_rolling_active_excludes_shadow_and_other_versions(self):
        c=candidate();r={**c,'model_schema':MODEL_SCHEMA,'source':'PAPER','quality':'OBSERVED_M1','net_r':2.,'opened_ts':T-120000,'closed_ts':T-60000,'trade_id':'a'}
        rows=[r,{**r,'source':'SHADOW','net_r':-4.},{**r,'source':'HISTORICAL_REPLAY','net_r':-4.},{**r,'model_schema':'MEXC_OPPORTUNITY_15_4','net_r':-4.},{**r,'closed_ts':T+1,'net_r':-4.}]
        self.assertEqual(active_estimate(rows,c,T),active_estimate([r],c,T)); self.assertGreater(active_estimate(rows,c,T)['expected_r_estimate'],0)
        self.assertEqual(active_estimate(rows[1:],c,T)['total_n'],0)
    def test_short_retest_does_not_detect_older_break(self):
        rows=[ms.Bar(T+i*300000,100,100.1,99.9,100,100) for i in range(35)]
        rows[25]=ms.Bar(rows[25].ts,100.,101.2,99.99,101.1,200)
        for i in range(26,34):rows[i]=ms.Bar(rows[i].ts,101.,101.2,100.7,101.,100)
        rows[34]=ms.Bar(rows[34].ts,100.1,100.7,100.05,100.6,100)
        args=dict(side=1,atr=.4,tick=.01,min_rvol=1.1,trend=True,zone=None,timeframe='M5')
        self.assertFalse(any(x['family']=='BREAKOUT_RETEST' for x in detect(rows,**args,retest_window=4)))
        self.assertTrue(any(x['family']=='BREAKOUT_RETEST' for x in detect(rows,**args,retest_window=12)))
    def test_shadow_families_never_enter_even_with_ready_status(self):
        for fam in ('STRUCTURE_CONTINUATION','COUNTERTREND_WAVE'):
            with patch.dict('os.environ',{'BZU_ALLOW_M1_ENTRY':'false'}): r=ms.analyze_instrument(**fixture(-1,'LINK_USDT'))
            r.update(family=fam,status='READY'); m={}; st={'instruments':[r],'defaults':{'deposit_usdt':100,'risk_pct':3,'max_leverage':5}}
            ms.update_dual_paper_slots(st,m,now_ms=r['quote_ts']); self.assertIsNone(m.get('cm_active'));self.assertEqual(tag_experiment({'family':fam})['research_variant'],fam)
    def test_research_variants_never_replace_active(self):
        kw=fixture(-1,'LINK_USDT')
        with patch.dict('os.environ',{'BZU_ALLOW_M1_ENTRY':'false'}):
            r=ms.analyze_instrument(**kw)
            real=ms.detect
            def det(rows,**args):
                base=real(rows,**args)
                if args.get('retest_window')==12 and args['side']==r['side'] and rows[-1].ts==r['trigger_ts']:
                    base+=[{k:r[k] for k in ('family','setup','setup_ua','base_score','structural_stop','reference','trigger_ts','trigger_tf','rvol','retest_level')}]
                    base[-1].update(family='BREAKOUT_RETEST',setup='BREAKOUT_RETEST',base_score=99,retest_level=99.)
                return base
            with patch.object(ms,'detect',side_effect=det): q=ms.analyze_instrument(**kw)
        self.assertEqual(q['signal_id'],r['signal_id']);self.assertEqual(q['entry'],r['entry']);self.assertTrue(any(d.get('research_variant')=='RETEST_12' and d['status']=='WATCH' for d in q['candidate_diagnostics']))
    def test_paired_exits_and_no_active_mutation(self):
        for side in (-1,1):
            c=candidate(side);_,ep=exit_policies('MOMENTUM','IMPULSE',2.,'NEUTRAL')
            c.update(research_variant='STRUCTURE_CONTINUATION',shadow_exit_policy=ep,geometry_policy=POLICY,trail_atr_m5=.25,trail_start_r=1.25,rejection_codes=['RESEARCH_ONLY'])
            m={'oil_history':[{'net_r':-1.}],'consumed_signals':{'x':T},'oil_active':{'symbol':'sentinel'}};before=copy.deepcopy(m)
            x={'symbol':c['symbol'],'analysis_fresh':True,'candidate_diagnostics':[c],'candles_1m':[]}
            shadow_step(m,[x],T);register(m,[x],T);register(m,[x],T)
            pending=m['opportunity_ledger']['pending'];self.assertEqual(len(pending),2)
            first=bar(T,100,101.3,99.9,101.25) if side==1 else bar(T,100,100.1,98.7,98.75)
            shadow_step(m,[{**x,'candles_1m':[first]}],T+60000)
            by={a['shadow_variant']:a for a in pending.values()};self.assertTrue(by['V154_EXIT']['tp1_hit']);self.assertFalse(by['CONTROL_EXIT']['tp1_hit'])
            second=bar(T+60000,101.25,101.3,99.9,100.) if side==1 else bar(T+60000,98.75,100.1,98.7,100.)
            shadow_step(m,[{**x,'candles_1m':[first,second]}],T+120000)
            self.assertEqual(len(pending),1);self.assertEqual(m['opportunity_ledger']['resolved'][0]['reason'],'TP1_BE_STOP')
            third=bar(T+120000,100.,102.1,99.9,102.) if side==1 else bar(T+120000,100.,100.1,97.9,98.)
            shadow_step(m,[{**x,'candles_1m':[first,second,third]}],T+180000)
            self.assertEqual(len(pending),0);self.assertEqual(research_summary(m['opportunity_ledger'])['paired_exit_comparison']['STRUCTURE_CONTINUATION']['pairs'],1)
            self.assertEqual({k:m[k] for k in before},before)
    def test_alpha_decay_runs_only_in_shadow_challenger(self):
        c=candidate();_,ep=exit_policies('MOMENTUM','IMPULSE',2.,'NEUTRAL')
        c.update(shadow_exit_policy=ep,geometry_policy=POLICY,trail_atr_m5=.25,trail_start_r=1.25)
        m={};x={'symbol':c['symbol'],'analysis_fresh':True,'candidate_diagnostics':[c],'candles_1m':[]}
        shadow_step(m,[x],T);register(m,[x],T)
        rows=[bar(T+i*60000,100,100.02,99.98,100) for i in range(75)]
        shadow_step(m,[{**x,'candles_1m':rows}],T+75*60000)
        ledger=m['opportunity_ledger'];self.assertEqual(len(ledger['pending']),1)
        self.assertEqual(ledger['resolved'][0]['shadow_variant'],'V154_EXIT');self.assertEqual(ledger['resolved'][0]['reason'],'STALE_EXIT')
        self.assertEqual(next(iter(ledger['pending'].values()))['shadow_variant'],'CONTROL_EXIT')

    def test_legacy_open_policy_not_rewritten(self):
        with patch.dict('os.environ',{'BZU_ALLOW_M1_ENTRY':'false'}): r=ms.analyze_instrument(**fixture(-1,'LINK_USDT'))
        m={}; st={'instruments':[r],'defaults':{'deposit_usdt':100,'risk_pct':3,'max_leverage':5}}
        now=r['quote_ts'];ms.update_dual_paper_slots(st,m,now_ms=now);a=m['cm_active'];a.update(model_schema='MEXC_OPPORTUNITY_15_4',tp1_enabled=True,time_stop_enabled=True)
        old={k:a[k] for k in ('entry','initial_stop','tp','risk','tp1_enabled','time_stop_enabled','model_schema')}
        ms.update_dual_paper_slots(st,m,now_ms=now);self.assertEqual({k:m['cm_active'][k] for k in old},old)

if __name__=='__main__':unittest.main()
