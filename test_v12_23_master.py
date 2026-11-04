"""As-of event scoring, protected geometry, 5m brake release and cadence."""
import copy
import unittest
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

import bot_oneshot as core
import bzu_policy as policy
import directional_policy as direction
import entry_adverse_selection as adverse
import full_limit_engine as engine
import ict_methodology as ict
import setup_scanner as scanner
import signal_quality as quality
from test_v12_22_precision import NOW, bar, fixture, ledger, advance, mirror


def master_fixture(side=1):
    # Reuse a witnessed BOS/FVG, translated onto a different price grid.
    # Temporarily disable only snapping while constructing the old fixture;
    # the actual planner and analyzer below use real protected geometry.
    with patch.object(policy,'round_target',side_effect=lambda e,t,s,k:t):
        snap,cfg,_,anchor,_=fixture(side)
    offset=.1*side
    snap.candles={tf:[replace(c,open=c.open+offset,high=c.high+offset,
                             low=c.low+offset,close=c.close+offset) for c in rows]
                  for tf,rows in snap.candles.items()}
    snap.price+=offset;snap.bid+=offset;snap.ask+=offset
    anchor.level+=offset;anchor.invalidation+=offset
    anchor.evidence=copy.deepcopy(anchor.evidence)
    for key in ('low','high'):anchor.evidence['entry_zone'][key]+=offset
    with engine.market_clock(core,NOW):
        ctx=engine.build_full_context(snap,engine.new_state(cfg),cfg)
    target={'kind':'test','level':103.1 if side==1 else 96.9,
            'distance':3.,'hard_barrier':False}
    with patch.object(engine,'full_targets',return_value=[target]):
        plan,reason=engine.full_plan(anchor,ctx,snap,cfg,{})
    if plan is None:raise AssertionError(reason)
    return snap,cfg,ctx,anchor,plan,target


def micro_snapshot(side=1):
    start=NOW-900_000
    five=[bar(start,104.,104.02,102.75,102.8),
          bar(start+300_000,102.8,102.85,101.82,101.85),
          bar(start+600_000,101.85,101.93,101.70,101.90)]
    shock=bar(start,104.,104.02,101.7,101.9)
    if side==-1:five=[mirror(c) for c in five];shock=mirror(shock)
    zone={'low':101.7,'high':101.9} if side==1 else {'low':98.1,'high':98.3}
    return engine.Snapshot(NOW,shock.close,{'15m':[shock],'5m':five}),zone


class EventVolume(unittest.TestCase):
    def setUp(self):
        self.rows=[replace(bar(NOW-(21-i)*900_000),volume=100.) for i in range(21)]
        self.snap=engine.Snapshot(NOW,100.,{'15m':self.rows})
        self.plan={'side':1,'entry_zone':{'low':99.,'high':100.,'kind':'FVG'},
                   'zone_origin':{'confirmed':True,'timeframe':'15m',
                     'break_known_ts':NOW,'displacement_known_ts':NOW},
                   'htf_1h':1,'htf_4h':1}
        self.cfg=engine.Config()

    def score(self):
        with patch.object(ict,'session_liquidity',return_value={'active':True}),patch.object(ict,'session_sweeps',return_value=[]):
            return quality.confluence(self.plan,self.snap,{'displacement_bos_fvg':True},self.cfg,engine)

    def test_event_excluded_from_20_bar_average_and_exact_threshold(self):
        self.rows[-1]=replace(self.rows[-1],volume=135.)
        result=self.score()
        self.assertEqual(result['volume_footprint']['bonus'],15)
        self.assertEqual(result['score'],80)
        self.assertEqual(scanner.event_rvol(self.snap,'15m',NOW)['sma_volume_20'],100.)

    def test_low_bos_loses_15_and_cannot_pass_65(self):
        self.rows[-1]=replace(self.rows[-1],volume=69.)
        result=self.score()
        self.assertEqual(result['score'],50)
        self.assertFalse(result['accepted'])
        self.rows[-1]=replace(self.rows[-1],volume=70.)
        self.assertEqual(self.score()['score'],65)

    def test_later_or_unclosed_volume_cannot_change_event_score(self):
        before=scanner.event_rvol(self.snap,'15m',NOW)
        self.rows.extend([replace(bar(NOW),volume=100000.),replace(bar(NOW-900_000,confirmed=False),volume=99999.)])
        self.assertEqual(scanner.event_rvol(self.snap,'15m',NOW),before)

    def test_gap_or_zero_baseline_abstains(self):
        del self.rows[8]
        self.assertIsNone(scanner.event_rvol(self.snap,'15m',NOW)['rvol'])
        self.snap.candles['15m']=[replace(c,volume=0.) for c in self.rows]
        self.assertEqual(self.score()['volume_footprint']['bonus'],0)

    def test_unrelated_later_impulse_is_not_a_volume_footprint(self):
        self.rows[-1]=replace(self.rows[-1],volume=10000.)
        self.plan['zone_origin'].update(displacement_known_ts=NOW-900_000,break_known_ts=NOW-900_000)
        self.assertEqual(self.score()['volume_footprint']['bonus'],0)

    def test_sweep_bonus_is_counted_once_and_score_capped(self):
        self.rows[-1]=replace(self.rows[-1],volume=200.)
        result=quality.volume_footprint(self.plan,self.snap,
            {'sweep_reclaim':True,'timeframe':'15m','latest_sweep':{'known_ts':NOW}},
            [{'sweep_ts':NOW},{'sweep_ts':NOW}])
        self.assertEqual(result['bonus'],15)
        self.assertEqual(len(result['events']),3)
        self.assertIsNone(scanner.event_rvol(self.snap,'15m',NOW+900_000)['rvol'])


class Vwap(unittest.TestCase):
    def test_utc_midnight_reset_exact_weighting_and_side_priority(self):
        start=NOW//86_400_000*86_400_000
        rows=[replace(bar(start+i*900_000,100.,100.,100.,100.),volume=1.) for i in range(48)]
        rows[-1]=replace(rows[-1],open=102.,high=102.,low=102.,close=102.,volume=3.)
        snap=engine.Snapshot(NOW,102.,{'15m':[bar(start-900_000,500.,500.,500.,500.),*rows]})
        v=direction.session_vwap(snap)
        self.assertAlmostEqual(v['value'],(47*100+3*102)/50)
        self.assertEqual(v['start_ts'],start)
        self.assertTrue(direction.vwap_priority({'side':1,'entry':101.},snap,.01)['aligned'])
        self.assertFalse(direction.vwap_priority({'side':-1,'entry':101.},snap,.01)['aligned'])
        snap.now=start+86_400_000
        self.assertIsNone(direction.session_vwap(snap)['value'])

    def test_missing_midnight_and_zero_volume_do_not_grant_a_plus(self):
        snap=engine.Snapshot(NOW,100.,{'15m':[bar(NOW-900_000)]})
        self.assertEqual(direction.vwap_priority({'side':1,'entry':1000.},snap,.01)['priority_points'],0)
        start=NOW//86_400_000*86_400_000
        snap.candles['15m']=[replace(bar(start+i*900_000),volume=0.) for i in range(48)]
        self.assertEqual(direction.session_vwap(snap)['status'],'NO_VOLUME')

    def test_minutes_do_not_double_count_and_forming_bars_are_excluded(self):
        start=NOW-900_000
        snap=engine.Snapshot(start+17*60_000,100.,{'15m':[
            bar(NOW//86_400_000*86_400_000+i*900_000) for i in range(48)],
            '1m':[bar(start+i*60_000) for i in range(18)]})
        result=direction.session_vwap(snap)
        self.assertEqual(result['status'],'AVAILABLE')
        self.assertEqual(result['volume'],47+17)
        self.assertEqual(result['known_ts'],snap.now)


class RoundLevels(unittest.TestCase):
    def test_requested_tp_examples_and_terminal_barrier(self):
        self.assertEqual(policy.round_target(74.20,75.03,1,.01),74.97)
        self.assertEqual(policy.round_target(74.80,73.97,-1,.01),74.03)
        self.assertEqual(policy.round_target(74.20,74.40,1,.01),74.40)
        self.assertEqual(policy.round_target(75.20,73.90,-1,.001),74.03)

    def test_stops_only_move_outward_and_avoid_exact_round_price(self):
        for side,stop,expected in ((1,74.,73.95),(1,73.99,73.94),(-1,74.,74.05),(-1,74.01,74.06)):
            actual=policy.round_stop(stop,side,.01)
            self.assertAlmostEqual(actual,expected)
            self.assertGreaterEqual(side*(stop-actual),.05-1e-8)
        self.assertEqual(policy.round_stop(73.92,1,.01),73.92)

    def test_planner_rechecks_rr_after_protection(self):
        with patch.object(policy,'round_target',side_effect=lambda e,t,s,k:t):
            snap,cfg,ctx,a,_=fixture()
        with patch.object(engine,'full_targets',return_value=[{'level':103.} ]):
            plan,reason=engine.full_plan(a,ctx,snap,cfg,{})
        self.assertIsNone(plan)
        self.assertEqual(reason,'ROUND_LEVEL_RR_TOO_LOW')

    def test_stop_over_risk_cap_is_rejected_instead_of_clipped(self):
        snap,cfg,ctx,a,_,target=master_fixture()
        with patch.object(policy,'technical_geometry',return_value=(100.36,99.62,.74,[{'name':'L1','price':100.36,'fraction':1.,'status':'PENDING'}])),patch.object(policy,'round_stop',return_value=99.56):
            self.assertEqual(engine.full_plan(a,ctx,snap,cfg,{})[1],'STRUCTURAL_STOP_TOO_WIDE')

    def test_targets_survive_fill_on_exact_ticks_both_sides(self):
        for side in (-1,1):
            _,cfg,_,_,plan,_=master_fixture(side)
            expected=(plan['tp1'],plan['tp2'])
            state=ledger(plan,cfg);en=plan['entry']
            candle=bar(NOW,en+.05,en+.07,en-.01,en+.02) if side==1 else bar(NOW,en-.05,en+.01,en-.07,en-.02)
            advance(state,cfg,[candle])
            active=engine.active_plans(state)[0]
            self.assertEqual((active['tp1'],active['tp2']),expected)
            self.assertEqual(active['initial_stop'],plan['stop'])

    def test_protected_early_tp1_does_not_lower_one_r_breakeven_trigger(self):
        _,cfg,_,_,p,_=master_fixture()
        state=ledger(p,cfg);en=p['entry']
        advance(state,cfg,[bar(NOW,en+.05,en+.07,en-.01,en+.02),
            bar(NOW+60000,en+.02,p['tp1']+.01,en+.01,p['tp1'])])
        active=engine.active_plans(state)[0]
        self.assertTrue(active['tp1_hit'])
        self.assertFalse(active.get('breakeven_armed',False))
        self.assertFalse(active.get('pending_stop'))
        self.assertEqual(active['stop'],p['stop'])

    def test_full_result_uses_protected_targets_actual_risk_and_fees(self):
        for side in (-1,1):
            _,cfg,_,_,p,_=master_fixture(side)
            state=ledger(p,cfg);en=p['entry']
            if side==1:
                rows=[bar(NOW,en+.05,en+.07,en-.01,en+.02),
                      bar(NOW+60000,en+.02,p['tp1']+.01,en+.01,p['tp1']),
                      bar(NOW+120000,p['tp1'],p['tp2']+.01,p['tp1']-.01,p['tp2'])]
            else:
                rows=[bar(NOW,en-.05,en+.01,en-.07,en-.02),
                      bar(NOW+60000,en-.02,en-.01,p['tp1']-.01,p['tp1']),
                      bar(NOW+120000,p['tp1'],p['tp1']+.01,p['tp2']-.01,p['tp2'])]
            advance(state,cfg,rows)
            trade=state['trades'][0]
            self.assertAlmostEqual(trade['gross_r'],.5*p['rr1']+.5*p['rr2'])
            self.assertGreater(trade['fees_r'],0)
            self.assertAlmostEqual(trade['net_r'],trade['gross_r']-trade['fees_r'])


class MicroBrake(unittest.TestCase):
    def test_final_engulfing_also_releases_both_sides(self):
        for side in (-1,1):
            snap,zone=micro_snapshot()
            start=NOW-900_000
            snap.candles['5m']=[bar(start,104.,104.02,101.58,101.6),
                bar(start+300_000,101.6,101.62,101.54,101.55),
                bar(start+600_000,101.55,101.72,101.5,101.7)]
            snap.candles['15m']=[bar(start,104.,104.02,101.5,101.7)]
            zone={'low':101.5,'high':101.7}
            if side==-1:
                snap.candles={tf:[mirror(c) for c in rows] for tf,rows in snap.candles.items()}
                zone={'low':98.3,'high':98.5}
            self.assertFalse(adverse.approach(snap,1.,side,zone,.01)['blocked'])

    def test_last_closed_5m_pin_releases_same_15m_brake_both_sides(self):
        for side in (-1,1):
            snap,zone=micro_snapshot(side)
            result=adverse.approach(snap,1.,side,zone,.01)
            self.assertFalse(result['blocked'])
            self.assertEqual(result['confirmation_timeframe'],'5m')
            self.assertEqual(result['confirmation_known_ts'],NOW)

    def test_missing_open_future_or_inconsistent_5m_cannot_release(self):
        for mutation in ('missing','unclosed','future','inconsistent'):
            snap,zone=micro_snapshot()
            if mutation=='missing':snap.candles['5m'].pop(0)
            elif mutation=='unclosed':snap.candles['5m'][-1]=replace(snap.candles['5m'][-1],confirmed=False)
            elif mutation=='future':snap.now=NOW-1
            else:snap.candles['5m'][-1]=replace(snap.candles['5m'][-1],low=101.)
            # At NOW-1 the 15m shock isn't closed and cannot activate a brake.
            if mutation=='future':
                self.assertIsNone(adverse.intra_bar_reaction(snap,snap.candles['15m'][0],1,zone,1.,.01))
            else:self.assertTrue(adverse.approach(snap,1.,1,zone,.01)['blocked'],mutation)

    def test_stale_or_wrong_zone_reaction_cannot_release(self):
        snap,zone=micro_snapshot();snap.now+=900_000
        self.assertTrue(adverse.approach(snap,1.,1,zone,.01)['blocked'])
        snap.now=NOW
        self.assertTrue(adverse.approach(snap,1.,1,{'low':100.,'high':100.2},.01)['blocked'])

    def test_complete_minute_aggregation_and_no_gap_fallback(self):
        snap,zone=micro_snapshot()
        minutes=[]
        for c in snap.candles['5m']:
            for i in range(5):
                o=c.open+(c.close-c.open)*i/5;close=c.open+(c.close-c.open)*(i+1)/5
                minutes.append(replace(c,ts=c.ts+i*60_000,open=o,close=close,
                                       high=max(o,close) if i!=4 else c.high,
                                       low=min(o,close) if i!=4 else c.low,volume=.2))
        snap.candles['1m']=minutes
        self.assertEqual(len(scanner.closed_five_minutes(snap)),3)
        self.assertFalse(adverse.approach(snap,1.,1,zone,.01)['blocked'])
        snap.candles['1m'].pop(7)
        self.assertTrue(adverse.approach(snap,1.,1,zone,.01)['blocked'])

    def test_minute_aggregation_survives_snapshot_parse(self):
        rows=[bar(NOW-900_000+i*60_000) for i in range(15)]
        snap=engine.Snapshot.parse({'now':NOW,'price':100.,'candles':{'1m':[asdict(c) for c in rows]}})
        self.assertEqual(len(snap.candles['5m']),3)

    def test_explicit_archived_five_minutes_survive_parse_without_minutes(self):
        snap,zone=micro_snapshot()
        parsed=engine.Snapshot.parse({'now':NOW,'price':snap.price,
            'candles':{tf:[asdict(c) for c in rows] for tf,rows in snap.candles.items()}})
        self.assertFalse(adverse.approach(parsed,1.,1,zone,.01)['blocked'])


class Integration(unittest.TestCase):
    def test_same_interval_reaction_reaches_planner_now_and_waits_if_missing(self):
        for side in (-1,1):
            snap,cfg,ctx,a,_,target=master_fixture(side)
            micro,zone=micro_snapshot(side)
            offset=-1.5*side
            shift=lambda c:replace(c,open=c.open+offset,high=c.high+offset,
                                   low=c.low+offset,close=c.close+offset)
            snap.candles['15m'][-1]=shift(micro.candles['15m'][0])
            snap.candles['1m']=[]
            snap.candles['5m']=[shift(c) for c in micro.candles['5m']]
            snap.price=100.55 if side==1 else 99.45
            snap.bid=snap.price-.01;snap.ask=snap.price+.01
            with engine.market_clock(core,NOW):
                ctx=engine.build_full_context(snap,engine.new_state(cfg),cfg)
            with patch.object(engine,'full_targets',return_value=[target]):
                plan,why=engine.full_plan(a,ctx,snap,cfg,{})
            self.assertIsNotNone(plan,why)
            self.assertEqual(plan['placed_ts'],NOW)
            self.assertEqual(plan['action_type'],'NEW_PENDING_LIMIT')
            self.assertEqual(plan['approach']['confirmation_timeframe'],'5m')
            snap.candles['5m']=[]
            self.assertEqual(engine.full_plan(a,ctx,snap,cfg,{})[1],'IMPULSE_CONFIRMATION_REQUIRED')

    def test_real_analyzer_keeps_confirmed_long_and_short_after_gates(self):
        for side in (-1,1):
            snap,cfg,ctx,a,p,target=master_fixture(side)
            with patch.object(engine,'merge_full_anchors',return_value=[a]),patch.object(engine,'full_targets',return_value=[target]):
                result=engine.analyze(snap,cfg,engine.new_state(cfg))
            self.assertEqual(len(result['plans']),1,result['rejections'])
            plan=result['plans'][0]
            self.assertEqual(plan['policy'],policy.POLICY)
            self.assertIn('volume_footprint',plan['confluence'])
            self.assertIn('session_vwap',plan)
            self.assertGreaterEqual(plan['net_rr'],cfg.min_net_rr)

    def test_all_24_still_require_structural_trigger(self):
        self.assertEqual(len(core.DETECTORS),24)
        for name in core.DETECTED_SETUP_TYPES:
            self.assertFalse(ict.validate({'setup':name,'policy':policy.POLICY,
                'detector_confirmed':True,'pattern_proof':{'confirmed':True}},
                {'as_of_ts':NOW,'structure_shift':True,'near_unmitigated_fvg':True,
                 'displacement':True,'break_acceptance':True,'level_rejection':True,'reference_reclaim':True})[0],name)

    def test_old_quality_archived_once_without_erasing_old_positions(self):
        snap,cfg,_,_,plan,_=master_fixture()
        state=engine.new_state(cfg)
        state['active']=copy.deepcopy(plan);state['active']['policy']=policy.PRECISION_POLICY
        quality.ensure(state,cfg)
        state['quality']['strategy']='bzu-precision-confirmed-single-v11'
        state['quality']['observations']=[{'id':'old_evidence'}]
        old=copy.deepcopy(state['active'])
        quality.ensure(state,cfg);quality.ensure(state,cfg)
        self.assertEqual(state['active'],old)
        self.assertEqual(len(state['quality_archives']),1)
        self.assertEqual(state['quality_archives'][0]['observations'],[{'id':'old_evidence'}])

    def test_legacy_filled_plan_keeps_frozen_exit_contract(self):
        _,cfg,_,_,plan,_=master_fixture()
        plan.update(policy=policy.PRECISION_POLICY,entry_legs=[{'name':'L1','price':100.36,'fraction':1.,'status':'PENDING'}],
                    tp1=100.94,rr1=(100.94-100.36)/plan['risk'])
        state=ledger(plan,cfg)
        advance(state,cfg,[bar(NOW,100.4,100.42,100.35,100.38)])
        active=engine.active_plans(state)[0]
        self.assertAlmostEqual(active['tp1'],100.94)
        self.assertEqual(active['partials'],{'TP1':.5,'TP2':.5})

    def test_restart_preserves_old_pending_prices_without_resnapping(self):
        import tempfile
        _,cfg,_,_,plan,_=master_fixture()
        plan.update(policy=policy.PRECISION_POLICY,stop=100.,initial_stop=100.,
                    tp1=100.94,tp2=101.59,tp3=101.59,tp=101.59)
        state=ledger(plan,cfg)
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'state.json'
            engine.atomic_write(path,state)
            restored=engine.load_state(path,cfg)
        for key,value in plan.items():
            self.assertEqual(restored['pending'][key],value,key)

    def test_workflow_version_and_report_cadence_match(self):
        root=Path(__file__).resolve().parent
        main=(root/'.github/workflows/main.yml').read_text()
        self.assertEqual(main,(root/'WORKFLOW_main.yml').read_text())
        self.assertIn("cron: '*/15 * * * *'",main)
        self.assertIn(engine.VERSION,main)
        self.assertEqual(core.BOT_VERSION,engine.VERSION)
        self.assertIn('python bot_oneshot.py --auto-report',main)
        self.assertNotIn('continuous_worker.py',main)
        self.assertFalse(engine.report_due(NOW+899999,NOW))
        self.assertTrue(engine.report_due(NOW+900000,NOW))

    def test_replay_keeps_closed_minute_and_five_minute_evidence_without_future(self):
        data={'start_ts':NOW,'end_ts':NOW,'candles':{
            '3m':[asdict(bar(NOW-180_000))],
            '1m':[asdict(bar(NOW-60_000)),asdict(bar(NOW))],
            '5m':[asdict(bar(NOW-300_000)),asdict(bar(NOW))]}}
        seen=[]
        def capture(state,snapshot,cfg):
            seen.append(snapshot)
            return {'action':'WATCH'}
        with patch.object(engine,'run_cycle',side_effect=capture):
            engine.replay(data,engine.Config(core=core))
        self.assertEqual(len(seen),1)
        self.assertEqual([c.ts for c in seen[0].candles['1m']],[NOW-60_000])
        self.assertEqual([c.ts for c in seen[0].candles['5m']],[NOW-300_000])


if __name__=='__main__':unittest.main()
