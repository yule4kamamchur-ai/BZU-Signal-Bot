"""Release contracts: as-of entries, full risk, impulse brakes and restart safety."""
import copy
import math
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch

import bot_oneshot as core
import bzu_policy as policy
import directional_policy as direction
import entry_adverse_selection as adverse
import full_limit_engine as e
import ict_methodology as ict
import portfolio_arbitration as arbitration
import setup_scanner as scanner
import signal_quality as quality

NOW=int(datetime(2026,11,4,12,tzinfo=timezone.utc).timestamp()*1000)


def bar(ts,o=100.,h=100.1,l=99.9,c=100.,confirmed=True):
    return e.Candle(ts,o,h,l,c,1.,confirmed)


def mirror(c):
    return bar(c.ts,200-c.open,200-c.low,200-c.high,200-c.close,c.confirmed)


def fixture(side=1):
    rows={}
    for tf,step in e.CANDLE_TF.items():
        series=[]
        for i in range(80):
            p=(100.+.015*math.sin(i) if tf=='15m' else
               100.9 if tf in ('1m','3m') else 100.9-(79-i)*.12)
            series.append(bar(NOW-(80-i)*step,p-.01,p+.07,p-.07,p))
        rows[tf]=series
    rows['15m'][-3:]=[bar(NOW-2700000,100.,100.12,99.90,99.95),
        bar(NOW-1800000,99.95,100.85,99.94,100.80),
        bar(NOW-900000,100.80,100.95,100.40,100.85)]
    snap=e.Snapshot(NOW,100.9,rows,100.89,100.91,True,NOW,'BZ-USDT-SWAP')
    if side==-1:
        snap.candles={tf:[mirror(c) for c in bars] for tf,bars in rows.items()}
        snap.price=99.1;snap.bid=99.09;snap.ask=99.11
    cfg=e.Config(core=core,tick=.01,include_commission=True,slippage_bps=0.)
    zone={'low':100.12 if side==1 else 99.60,'high':100.40 if side==1 else 99.88,
          'known_ts':NOW,'kind':'FVG_BULL' if side==1 else 'FVG_BEAR','timeframe':'15m'}
    with e.market_clock(core,NOW):
        ctx=e.build_full_context(snap,e.new_state(cfg),cfg)
        anchor=core.make_anchor('FRESH_BASE_CONTINUATION','LONG' if side==1 else 'SHORT',
             100.26 if side==1 else 99.74,99.90 if side==1 else 100.10,
             'FVG','witnessed BOS FVG',ctx,80,evidence={
             'base_known_before_displacement':True,'setup_timeframe':'15m','entry_zone':zone})
    target={'kind':'1H_LIQUIDITY','level':103. if side==1 else 97.,'distance':3.,
            'known_ts':NOW-3600000,'timeframe':'1H','hard_barrier':False}
    with patch.object(e,'full_targets',return_value=[target]):
        p,reason=e.full_plan(anchor,ctx,snap,cfg,{})
    if p is None: raise AssertionError(reason)
    return snap,cfg,ctx,anchor,p


def ledger(p,cfg):
    s=e.new_state(cfg);s['pending']=copy.deepcopy(p)
    s['last_run_ts']=p['placed_ts']
    return s


def advance(s,cfg,rows):
    e.advance_lifecycle(s,e.Snapshot(rows[-1].ts+60000,rows[-1].close,{'1m':rows}),cfg)


class Precision(unittest.TestCase):
    def test_real_bos_zone_reaches_single_plan_both_sides(self):
        for side in (-1,1):
            snap,cfg,ctx,a,p=fixture(side)
            self.assertTrue(p['zone_origin']['confirmed'])
            self.assertEqual([l['fraction'] for l in p['entry_legs']],[1.])
            self.assertAlmostEqual(p['entry'],100.26 if side==1 else 99.74)
            self.assertGreaterEqual(p['rr1'],1.4)
            self.assertGreaterEqual(p['rr2'],2.5-1e-8)
            self.assertLessEqual(p['risk'],.75)
            self.assertEqual(p['partials'],{'TP1':.5,'TP2':.5})

    def test_narrow_front_and_deep_stop_never_clipped(self):
        cfg=e.Config(tick=.01)
        for side in (-1,1):
            swing=99.9 if side==1 else 100.2
            entry,stop,risk,legs=policy.technical_geometry(100.,100.1,swing,side,1.,cfg,e)
            self.assertAlmostEqual(entry,100.12 if side==1 else 99.98)
            self.assertGreaterEqual(side*(swing-stop),.2-1e-8)
            _,far_stop,risk,_=policy.technical_geometry(100.,100.1,98.9 if side==1 else 101.2,side,1.,cfg,e)
            self.assertGreater(risk,.75)
            self.assertGreaterEqual(side*((98.9 if side==1 else 101.2)-far_stop),.2-1e-8)

    def test_weak_zone_and_unknown_origin_cannot_place(self):
        snap,cfg,ctx,a,p=fixture()
        snap.candles['15m'][-3:]=[bar(NOW-2700000),bar(NOW-1800000),bar(NOW-900000)]
        self.assertEqual(e.full_plan(a,ctx,snap,cfg,{})[1],'ZONE_ORIGIN_BOS_REQUIRED')

    def test_unclosed_break_cannot_validate_zone(self):
        snap,cfg,ctx,a,p=fixture()
        snap.candles['15m'][-2]=replace(snap.candles['15m'][-2],confirmed=False)
        self.assertFalse(scanner.zone_origin(p['entry_zone'],snap,1,cfg.tick,e)['confirmed'])

    def test_empty_zone_score_and_trigger_do_not_pass(self):
        snap,cfg,ctx,a,p=fixture()
        p['edge_intelligence']={'status':'ALLOW'}
        with patch.object(ict,'session_sweeps',return_value=[]),patch.object(ict,'session_liquidity',return_value={'active':True}):
            result=quality.confluence(p,snap,{},cfg,e)
        self.assertEqual(result['score'],45)
        self.assertFalse(result['accepted'])
        self.assertEqual(result['reason'],'ICT_SWEEP_OR_BOS_FVG_REQUIRED')

    def test_sweep_and_bos_routes_score_and_countertrend_threshold(self):
        snap,cfg,ctx,a,p=fixture()
        with patch.object(ict,'session_sweeps',return_value=[]),patch.object(ict,'session_liquidity',return_value={'active':True}):
            bos=quality.confluence(p,snap,{'displacement_bos_fvg':True},cfg,e)
            self.assertEqual(bos['score'],65)
            self.assertTrue(bos['accepted'])
            p['htf_1h']=-1
            counter=quality.confluence(p,snap,{'displacement_bos_fvg':True},cfg,e)
            self.assertEqual(counter['threshold'],80)
            self.assertFalse(counter['accepted'])
            both=quality.confluence(p,snap,{'displacement_bos_fvg':True,'sweep_reclaim':True},cfg,e)
            self.assertTrue(both['accepted'])

    def test_all_24_precision_contracts_reject_empty_trigger(self):
        self.assertEqual(set(ict.CONTRACTS),set(core.DETECTED_SETUP_TYPES))
        for name in core.DETECTED_SETUP_TYPES:
            p={'setup':name,'policy':policy.POLICY,'detector_confirmed':True,
               'pattern_proof':{'confirmed':True}}
            proof={'as_of_ts':NOW,'structure_shift':True,'near_unmitigated_fvg':True,
                   'displacement':True,'break_acceptance':True,'level_rejection':True,'reference_reclaim':True}
            self.assertFalse(ict.validate(p,proof)[0],name)

    def test_full_analyzer_keeps_confirmed_long_and_short_after_all_gates(self):
        for side in (-1,1):
            snap,cfg,ctx,a,p=fixture(side)
            target={'kind':'test','level':103. if side==1 else 97.,'distance':3.,'hard_barrier':False}
            with patch.object(e,'merge_full_anchors',return_value=[a]),patch.object(e,'full_targets',return_value=[target]):
                result=e.analyze(snap,cfg,e.new_state(cfg))
            self.assertEqual(len(result['plans']),1,result['rejections'])
            accepted=result['plans'][0]
            self.assertTrue(accepted['quality']['accepted'])
            self.assertTrue(accepted['confluence']['trigger_confirmed'])
            self.assertGreaterEqual(accepted['score'],65)

    def test_merge_preserves_one_entry_and_does_not_sum_scores(self):
        snap,cfg,ctx,a,p=fixture()
        p.update(score=65,quality={'reason':'CONFIRMED'})
        other=copy.deepcopy(p);other.update(id='other',event_key='other',score=70)
        merged=policy.merge_plans([p,other],snap,cfg,e)
        self.assertEqual(len(merged),1)
        self.assertEqual(merged[0]['score'],70)
        self.assertEqual(len(merged[0]['entry_legs']),1)

    def test_partial_then_final_uses_full_risk_and_costs(self):
        for side in (-1,1):
            _,cfg,_,_,p=fixture(side);s=ledger(p,cfg)
            transform=(lambda c:c) if side==1 else mirror
            longp=p if side==1 else fixture(1)[-1]
            en=longp['entry'];t1=longp['tp1'];t2=longp['tp2']
            advance(s,cfg,[transform(bar(NOW,en+.1,en+.12,en-.01,en+.02)),
                transform(bar(NOW+60000,en+.02,t1+.01,en+.01,t1)),
                transform(bar(NOW+120000,t1,t2+.01,t1-.01,t2))])
            t=s['trades'][0]
            self.assertEqual(t['filled_fraction'],1.)
            self.assertAlmostEqual(t['realized_legs'][0]['fraction'],.5)
            self.assertAlmostEqual(t['gross_r'],.5*t['rr1']+.5*t['rr2'])
            self.assertGreater(t['fees_r'],0)
            self.assertAlmostEqual(t['net_r'],t['gross_r']-t['fees_r'])

    def test_one_r_be_does_not_close_any_volume_and_starts_next_bar(self):
        _,cfg,_,_,p=fixture();s=ledger(p,cfg);en=p['entry'];r=p['risk']
        advance(s,cfg,[bar(NOW,en+.1,en+.12,en-.01,en+.02),
                      bar(NOW+60000,en+.02,en+r+.01,en-.01,en+r)])
        active=e.active_plans(s)[0]
        self.assertEqual(active['remaining'],1.)
        self.assertEqual(active['realized_legs'],[])
        self.assertEqual(active['stop'],p['stop'])
        self.assertTrue(active['breakeven_armed'])
        self.assertEqual(active['pending_stop']['effective_ts'],NOW+120000)
        advance(s,cfg,[bar(NOW+120000,en+r,en+r+.01,en-.01,en)])
        self.assertGreaterEqual(s['trades'][0]['net_r'],-1e-8)

    def test_ambiguous_fill_and_stop_always_loss(self):
        _,cfg,_,_,p=fixture();s=ledger(p,cfg)
        advance(s,cfg,[bar(NOW,p['entry']+.1,p['tp2']+.1,p['stop']-.01,p['entry'])])
        self.assertLess(s['trades'][0]['net_r'],-1.)
        self.assertEqual(s['trades'][0]['filled_fraction'],1.)
        self.assertTrue(s['trades'][0]['ambiguous_ohlc'])

    def test_old_open_position_keeps_split_exit_contract(self):
        _,cfg,_,_,p=fixture();p.update(policy=policy.LEGACY_POLICY,rr1=1.,
             tp1=p['entry']+p['risk'],partials={'TP1':.6,'TP2':.4},exit_policy='TP1_60_BE_TP2_40')
        s=ledger(p,cfg)
        advance(s,cfg,[bar(NOW,p['entry']+.1,p['entry']+.12,p['entry']-.01,p['entry']),
                      bar(NOW+60000,p['entry'],p['tp1']+.01,p['entry']-.01,p['tp1'])])
        self.assertAlmostEqual(e.active_plans(s)[0]['remaining'],.4)


class Brakes(unittest.TestCase):
    def test_adverse_single_or_two_candles_need_later_reaction_both_sides(self):
        for side in (-1,1):
            for bodies in ((2.,),(1.,1.)):
                rows=[];o=104.
                for i,body in enumerate(bodies):
                    rows.append(bar(NOW-(len(bodies)-i)*900000,o,o+.02,o-body-.01,o-body));o-=body
                if side==-1: rows=[mirror(c) for c in rows]
                zone={'low':101.8,'high':102.2} if side==1 else {'low':97.8,'high':98.2}
                snap=e.Snapshot(NOW,o,{'15m':rows})
                self.assertTrue(adverse.approach(snap,1.,side,zone,.01)['blocked'])
                confirm=bar(NOW,102.,102.7,101.85,102.6)
                snap.candles['15m'].append(confirm if side==1 else mirror(confirm))
                self.assertTrue(adverse.approach(snap,1.,side,zone,.01)['blocked'])
                snap.now+=900000
                self.assertFalse(adverse.approach(snap,1.,side,zone,.01)['blocked'])

    def test_favorable_impulse_does_not_block(self):
        snap=e.Snapshot(NOW,103.,{'15m':[bar(NOW-900000,100.,103.,99.9,102.99)]})
        self.assertFalse(adverse.approach(snap,1.,1,{'low':99.,'high':100.})['blocked'])

    def test_catchup_requires_latest_closed_bullish_reclaim(self):
        snap,cfg,ctx,a,p=fixture();en=p['entry'];stop=p['stop']
        start=NOW-900000
        snap.price=en+.02;snap.bid=en+.01;snap.ask=en+.03
        snap.candles['1m']=[bar(start+i*60000,en+.02,en+.05,en-.01,en+.02) for i in range(15)]
        snap.candles['15m'][-1]=bar(start,en+.04,en+.1,en-.02,en+.02)
        zone={'low':en-.02,'high':en}
        self.assertFalse(scanner.catch_up(snap,en,stop,1,start,.01,zone)['allowed'])
        snap.candles['15m'][-1]=bar(start,en-.01,en+.1,en-.02,en+.02)
        self.assertTrue(scanner.catch_up(snap,en,stop,1,start,.01,zone)['allowed'])
        snap.ask=en-.01
        self.assertFalse(scanner.catch_up(snap,en,stop,1,start,.01,zone)['allowed'])

    def test_cooldown_exact_hour_and_new_sweep_exception(self):
        t={'side':1,'closed_ts':NOW,'close_reason':'STOP','net_r':-1.}
        p={'side':1,'score':90,'ict_evidence':{'latest_sweep':{'known_ts':NOW-1}}}
        self.assertFalse(arbitration.loss_cooldown(p,[t],NOW+3599999)['allowed'])
        self.assertTrue(arbitration.loss_cooldown(p,[t],NOW+3600000)['allowed'])
        p['ict_evidence']['latest_sweep']['known_ts']=NOW+60000
        self.assertTrue(arbitration.loss_cooldown(p,[t],NOW+60000)['allowed'])
        p['score']=81
        self.assertFalse(arbitration.loss_cooldown(p,[t],NOW+60000)['allowed'])
        p['side']=-1
        self.assertTrue(arbitration.loss_cooldown(p,[t],NOW+60000)['allowed'])

    def test_night_and_dst_open_boundaries(self):
        def ts(month,hour,minute):
            return int(datetime(2026,month,4,hour,minute,tzinfo=timezone.utc).timestamp()*1000)
        self.assertFalse(policy.session_gate(ts(11,21,30))['allowed'])
        self.assertFalse(policy.session_gate(ts(11,2,59))['allowed'])
        self.assertTrue(policy.session_gate(ts(11,3,0))['allowed'])
        for month,hour in ((11,14),(6,13)):
            self.assertFalse(policy.session_gate(ts(month,hour,30))['allowed'])
            self.assertTrue(policy.session_gate(ts(month,hour,45))['allowed'])

    def test_gap_over_night_cancels_before_fill_without_future_price(self):
        _,cfg,_,_,p=fixture()
        night=int(datetime(2026,11,4,21,30,tzinfo=timezone.utc).timestamp()*1000)
        p.update(placed_ts=night-60000,effective_ts=night-60000,expires_ts=night+3600000)
        s=ledger(p,cfg)
        advance(s,cfg,[bar(night,p['entry']+.1,p['entry']+.2,p['stop']-.1,p['entry'])])
        self.assertFalse(s['trades'])
        self.assertEqual(s['orders'][0]['reason'],'OIL_OVERNIGHT_BLACKOUT')

    def test_active_position_does_not_fill_second_prepublished_limit(self):
        snap,cfg,ctx,a,p=fixture();s=ledger(p,cfg)
        advance(s,cfg,[bar(NOW,p['entry']+.1,p['entry']+.2,p['entry']-.01,p['entry'])])
        other=copy.deepcopy(p);other.update(id='second',event_key='second',entry=p['entry']-.2)
        e.park_pending(s,[other])
        self.assertEqual(e.entry_priority(s,NOW+60000,p['entry'],cfg,60000),[])

    def test_pullback_conflict_blocks_ltf_but_reached_htf_can_be_reviewed(self):
        snap,cfg,ctx,a,p=fixture()
        regime={'state':'PULLBACK_PHASE','trend_state':'BULLISH_TREND','local_structure':{'side':-1}}
        self.assertEqual(direction.regime_admission(1,p['entry'],regime,snap,.01,e)[1],
                         'LOCAL_STRUCTURE_CONFLICT')
        zone={'timeframe':'1H','low':snap.price-.1,'high':snap.price+.1}
        self.assertTrue(direction.regime_admission(1,p['entry'],regime,snap,.01,e,zone)[0])

    def test_local_lower_lows_and_ema_are_causal_and_symmetric(self):
        rows=[]
        for i in range(64):
            price=110-.12*i+.30*math.sin(i*math.pi/3)
            rows.append(bar(NOW-(64-i)*900000,price+.02,price+.15,price-.15,price))
        for sign in (1,-1):
            bars=rows if sign==1 else [mirror(c) for c in rows]
            snap=e.Snapshot(NOW,bars[-1].close,{'15m':bars})
            expected=direction.local_structure(snap,.01,e)
            self.assertEqual(expected['side'],-sign)
            snap.candles['15m']=bars+[bar(NOW,150.,180.,90.,160.)]
            self.assertEqual(direction.local_structure(snap,.01,e),expected)

    def test_report_handles_conflict_and_new_exit_percentages(self):
        snap,cfg,ctx,a,p=fixture();p.update(score=85,rating='A+')
        regime={'state':'PULLBACK_PHASE','side_1h':1,'side_4h':1}
        import notification_report
        state=e.new_state(cfg)
        with patch.object(direction,'market_regime',return_value=regime):
            message=notification_report.build(state,snap,{'action':'PLACE_LIMIT','selected_plans':[p]},cfg,e)
        self.assertIn('CONFLICT / PULLBACK',message)
        self.assertIn('Limit 1 (100%)',message)
        self.assertIn('TP1 (50%)',message)
        self.assertNotIn('Limit 2',message)

    def test_old_pending_upgrade_preserves_levels_and_exit_terms(self):
        snap,cfg,ctx,a,p=fixture()
        old_cfg=replace(cfg,min_score=55,max_stop_absolute=.80)
        state=e.new_state(old_cfg)
        state['version']='full-ict-v12.21.0-bzu-checklist'
        p.update(policy=policy.LEGACY_POLICY,rr1=1.,partials={'TP1':.6,'TP2':.4},
                 exit_policy='TP1_60_BE_TP2_40')
        state['pending']=p
        frozen=copy.deepcopy(p)
        analysis={'health':[],'plans':[],'watch':[],'rejections':{},'contexts':{}}
        with patch.object(e,'analyze',return_value=analysis):
            e.run_cycle(state,snap,cfg)
        current=e.pending_plans(state)[0]
        for key in ('entry','stop','tp1','tp2','entry_legs','partials','exit_policy'):
            self.assertEqual(current[key],frozen[key],key)
        self.assertTrue(state['config_migrations'][0]['existing_plans_preserved'])

    def test_quality_upgrade_preserves_old_evidence(self):
        cfg=e.Config();s=e.new_state(cfg)
        s['quality']={'schema':quality.SCHEMA,'strategy':'bzu-checklist-confluence-split-v10',
                      'signature':'old','pending':[],'observations':[{'id':'historical'}]}
        quality.ensure(s,cfg)
        self.assertEqual(s['quality']['observations'],[])
        self.assertEqual(s['quality_archives'][0]['observations'][0]['id'],'historical')

    def test_research_copy_retains_the_same_tp1_and_fill_contract(self):
        snap,cfg,ctx,a,p=fixture();state=e.new_state(cfg)
        features=quality.features(p,snap,ctx,cfg,e)
        quality.watch_candidates(state,[p],{p['id']:{'features':features}},cfg)
        lean=state['quality']['pending'][0]['pending']
        candle=bar(NOW,p['entry']+.1,p['entry']+.12,p['entry']-.01,p['entry'])
        actual=policy.first_fill(p,candle,e,cfg,60000)
        observed=policy.first_fill(lean,candle,e,cfg,60000)
        self.assertEqual(observed['tp1'],actual['tp1'])
        self.assertEqual(observed['partials'],actual['partials'])
        self.assertEqual(observed['risk_budget'],actual['risk_budget'])


if __name__=='__main__':
    unittest.main()
