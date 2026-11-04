"""Behavioral checklist tests: chronology, real quantities, admission and restart.

No network, credentials, real orders, profit labels or implementation mirrors.
"""
import copy
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import bot_oneshot as core
import bzu_policy as policy
import directional_policy as direction
import full_limit_engine as e
import ict_methodology as ict
import limit_structure
import research_zone_policy_1220 as zones
import setup_scanner
import signal_quality as quality

NOW=int(datetime(2026,11,4,12,tzinfo=timezone.utc).timestamp()*1000)


def bar(ts, o=100.5, h=100.6, l=100.4, c=100.5):
    return e.Candle(ts,o,h,l,c,1.,True)


def snapshot(side=1):
    rows={}
    for tf,step in e.CANDLE_TF.items():
        count=80 if tf in ('1m','3m','15m') else 40
        slope=.15 if tf in ('1H','4H') else .002
        series=[]
        for i in range(count):
            price=100.5-(count-1-i)*slope
            series.append(bar(NOW-(count-i)*step,price-.02,price+.08,price-.08,price))
        rows[tf]=series
    snap=e.Snapshot(NOW,100.5,rows,100.49,100.51,True,NOW,'BZ-USDT-SWAP')
    if side==-1:
        snap.price=99.5;snap.bid=99.49;snap.ask=99.51
        snap.candles={tf:[bar(c.ts,200-c.open,200-c.low,200-c.high,200-c.close) for c in bars]
                      for tf,bars in rows.items()}
    return snap


def fixture(side=1):
    snap=snapshot(side)
    cfg=e.Config(core=core,tick=.01,include_commission=True,slippage_bps=0.)
    with e.market_clock(core,NOW):
        ctx=e.build_full_context(snap,e.new_state(cfg),cfg)
        anchor=core.make_anchor('FRESH_BASE_CONTINUATION','LONG' if side==1 else 'SHORT',
            100.10 if side==1 else 99.9,99.75 if side==1 else 100.25,
            'FVG','confirmed fixture zone',ctx,80,evidence={
                'base_known_before_displacement':True,'setup_timeframe':'15m',
                'entry_zone':{'low':100.0 if side==1 else 99.8,
                    'high':100.2 if side==1 else 100.,'known_ts':NOW-900_000,
                    'kind':'FVG_BULL' if side==1 else 'FVG_BEAR','timeframe':'15m'}})
    targets=[{'kind':'1H_LIQUIDITY','level':102. if side==1 else 98.,
              'distance':2.,'hard_barrier':False,'timeframe':'1H','known_ts':NOW-3600000}]
    with patch.object(e,'full_targets',return_value=targets):
        p,reason=e.full_plan(anchor,ctx,snap,cfg,{})
    if p is None: raise AssertionError(reason)
    p.update(score=70.,rating='B',quality={'accepted':True,'reason':'CONFIRMED',
        'features':quality.features(p,snap,ctx,cfg,e),'statistics':{'ranking_adjustment':0}},
        decision_score=70.,direction_score=70.,level_key='fixture')
    return snap,cfg,ctx,anchor,p


def book(plan,cfg):
    state=e.new_state(cfg);state['pending']=copy.deepcopy(plan)
    state['last_run_ts']=plan['placed_ts'];return state


def advance(state,cfg,bars):
    snap=e.Snapshot(bars[-1].ts+60_000,bars[-1].close,{'1m':bars})
    e.advance_lifecycle(state,snap,cfg)
    return snap


class Admission(unittest.TestCase):
    def test_missing_minute_blocks_new_admission_even_when_flat(self):
        snap=snapshot();snap.candles['1m'].pop(-5)
        self.assertIn('GAP_1m',e.health(snap,e.Config()))

    def test_minimum_stop_applies_to_both_entry_halves(self):
        cfg=e.Config(tick=.01)
        for side in (-1,1):
            swing=100.02 if side==1 else 100.18
            first,stop,risk,legs=policy.technical_geometry(100.,100.2,swing,side,.12,cfg,e)
            self.assertTrue(all(side*(leg['price']-stop)>=.20-1e-9 for leg in legs))
            self.assertLessEqual(risk,.80)

    def test_long_short_front_run_split_structural_stop_and_real_tp(self):
        for side in (-1,1):
            snap,cfg,ctx,anchor,p=fixture(side)
            self.assertAlmostEqual(p['entry'],100.23 if side==1 else 99.77)
            self.assertEqual([x['fraction'] for x in p['entry_legs']],[.5,.5])
            self.assertAlmostEqual(p['entry_legs'][1]['price'],100.10 if side==1 else 99.90)
            self.assertGreater(side*(anchor.invalidation-p['stop']),0)
            self.assertTrue(.20<=p['risk']<=.80)
            self.assertAlmostEqual(side*(p['tp1']-p['entry'])/p['risk'],1.)
            self.assertGreaterEqual(p['rr2'],1.8)
            self.assertEqual(p['expires_ts']-p['effective_ts'],120*60_000)

    def test_fast_approach_uses_equilibrium_without_veto(self):
        snap,cfg,ctx,a,_=fixture()
        with patch('entry_adverse_selection.approach',return_value={'fast':True}), \
             patch.object(e,'full_targets',return_value=[{'kind':'test','level':102.,'distance':2.}]):
            p,reason=e.full_plan(a,ctx,snap,cfg,{})
        self.assertIsNotNone(p,reason)
        self.assertAlmostEqual(p['entry'],100.10)

    def test_current_candle_stop_breach_invalidates_even_if_quote_recovers(self):
        snap,cfg,ctx,a,p=fixture()
        snap.metadata['current_candle_1m']={'ts':NOW,'low':p['stop']-.01,'high':101.}
        self.assertEqual(e.full_plan(a,ctx,snap,cfg,{})[1],'ANCHOR_INVALIDATED')

    def test_rr_and_maximum_distance_are_real_gates(self):
        snap,cfg,ctx,a,p=fixture()
        with patch.object(e,'full_targets',return_value=[{'kind':'near','level':100.7,'distance':.47}]):
            self.assertEqual(e.full_plan(a,ctx,snap,cfg,{})[1],'TARGET_RR_TOO_LOW')
        snap.price=105.;snap.bid=104.99;snap.ask=105.01
        self.assertEqual(e.full_plan(a,ctx,snap,cfg,{})[1],'LIMIT_UNREACHABLE')

    def test_score_threshold_components_and_models_are_advisory(self):
        snap,cfg,ctx,a,p=fixture()
        p.update(meta_decision={'validated':True,'accepted':False,'expected_net_r':-.5},
                 edge_intelligence={'status':'REJECT'})
        proof={'sweep_reclaim':False}
        with patch.object(ict,'session_sweeps',return_value=[]), \
             patch.object(ict,'session_liquidity',return_value={'active':False}):
            score=quality.confluence(p,snap,proof,cfg,e)
        self.assertEqual(score['score'],55)
        self.assertTrue(score['accepted'])
        p['htf_1h']=0
        with patch.object(ict,'session_sweeps',return_value=[]), \
             patch.object(ict,'session_liquidity',return_value={'active':False}):
            low=quality.confluence(p,snap,proof,cfg,e)
        self.assertEqual(low['score'],40)
        self.assertFalse(low['accepted'])

    def test_all_24_method_contracts_require_actual_confirmation(self):
        yes={'sweep_reclaim':True,'structure_shift':True,'displacement':True,
             'near_unmitigated_fvg':True,'level_rejection':True,'break_acceptance':True,
             'reference_reclaim':True,'sweep_then_shift':True,'as_of_ts':NOW}
        no={k:False for k in yes};no['as_of_ts']=NOW
        self.assertEqual(set(ict.CONTRACTS),set(core.DETECTED_SETUP_TYPES))
        for name in core.DETECTED_SETUP_TYPES:
            p={'setup':name,'detector_confirmed':True,'pattern_proof':{'confirmed':True}}
            self.assertTrue(ict.validate(p,yes)[0],name)
            p['pattern_proof']={}
            self.assertFalse(ict.validate(p,no)[0],name)

    def test_full_analyzer_does_not_veto_a_55_point_confirmed_plan(self):
        snap,cfg,ctx,a,p=fixture()
        # Isolate actual admission stages, not the already-tested detector scans.
        real=core.DETECTORS
        detectors=[]
        for i,d in enumerate(real):
            def detect(context,index=i):
                return copy.deepcopy(a) if index==list(setup_scanner.SETUP_NAMES).index(a.setup_type) else None
            detect.__name__=d.__name__;detectors.append(detect)
        with patch.object(core,'DETECTORS',detectors), \
             patch.object(e,'full_targets',return_value=[{'kind':'test','level':102.,'distance':2.,'hard_barrier':False}]), \
             patch.object(e.meta,'evaluate',return_value={'accepted':False,'reason':'MODEL_REJECT',
                'probability':None,'status':'TEST','score_adjustment':-100}), \
             patch.object(e.edge_intelligence,'assess',return_value={'status':'REJECT','reason':'EDGE_REJECT'}):
            result=e.analyze(snap,cfg,e.new_state(cfg))
        self.assertTrue(result['plans'],result['rejections'])
        self.assertTrue(all(p['score']>=55 and p['quality']['accepted'] for p in result['plans']))


class MinuteLifecycle(unittest.TestCase):
    def test_two_prepublished_zones_share_two_slots_not_four_legs(self):
        snap,cfg,ctx,a,p=fixture()
        state=book(p,cfg)
        second=copy.deepcopy(p)
        second.update(id='second-zone',event_key='second-event',zone_key='second-zone',
            entry=99.8,limit_price=99.8,stop=99.4,initial_stop=99.4,invalidation=99.45,
            risk=.4,tp1=100.2,tp2=101.,tp=101.,target_cancel_armed=False,
            entry_legs=[{'name':'L1','price':99.8,'fraction':.5,'status':'PENDING'},
                        {'name':'L2','price':99.6,'fraction':.5,'status':'PENDING'}])
        state['second_slot']['pending']=second
        advance(state,cfg,[bar(NOW,100.5,100.6,99.75,100.4)])
        self.assertEqual(e.occupied_slots(state),2)
        self.assertEqual(len(e.active_plans(state)),2)
        self.assertEqual({pos['side'] for pos in e.active_plans(state)},{1})
        self.assertFalse(e.pending_plans(state))

    def test_pending_ttl_does_not_force_an_open_position_to_exit_before_tp1(self):
        snap,cfg,ctx,a,p=fixture()
        state=book(p,cfg)
        advance(state,cfg,[bar(NOW,100.5,100.55,100.22,100.4)])
        advance(state,cfg,[bar(NOW+12*3600_000,100.4,100.5,100.3,100.4)])
        self.assertIsNotNone(state['active'])
        self.assertFalse(state['trades'])
        self.assertFalse(state['active'].get('tp1_hit'))

    def test_placement_inside_touched_minute_requires_reconciliation(self):
        snap,cfg,ctx,a,p=fixture()
        p.update(placed_ts=NOW+15_000,effective_ts=NOW+60_000)
        state=book(p,cfg)
        advance(state,cfg,[bar(NOW,100.5,100.55,100.22,100.4)])
        self.assertFalse(state['trades'])
        self.assertFalse(state.get('active'))
        self.assertEqual(state['reconciliation']['reason'],'PLACEMENT_INSIDE_TOUCHED_BAR')
        self.assertEqual(e.occupied_slots(state),1)

    def test_wick_fill_tp1_then_breakeven_between_reports(self):
        snap,cfg,ctx,a,p=fixture()
        state=book(p,cfg)
        # Only L1 fills. Its wick is gone by the 15m closing quote.
        bars=[bar(NOW,100.5,100.55,100.22,100.4),
              bar(NOW+60_000,100.4,p['tp1']+.01,100.3,p['tp1']),
              bar(NOW+120_000,p['tp1'],p['tp1']+.01,100.24,100.3)]
        advance(state,cfg,bars)
        self.assertEqual(len(state['trades']),1)
        t=state['trades'][0]
        self.assertTrue(t['tp1_hit'])
        self.assertEqual(t['filled_fraction'],.5)
        self.assertAlmostEqual(t['realized_legs'][0]['fraction'],.3)
        self.assertEqual(t['entry_legs'][1]['status'],'CANCELLED_AFTER_TP1')
        self.assertGreater(t['net_r'],0)
        self.assertTrue(any(ev['kind']=='TP1_PARTIAL' for ev in state['events']))
        # Replaying after reload cannot book PnL twice.
        loaded=json.loads(json.dumps(state));advance(loaded,cfg,bars)
        self.assertEqual(loaded['trades'],state['trades'])

    def test_both_limits_fill_at_actual_weighted_price_and_one_trade(self):
        snap,cfg,ctx,a,p=fixture()
        state=book(p,cfg)
        advance(state,cfg,[bar(NOW,100.5,100.55,100.09,100.4)])
        position=state['active']
        self.assertIsNotNone(position)
        self.assertAlmostEqual(position['entry'],100.165)
        self.assertEqual(position['filled_fraction'],1.)
        advance(state,cfg,[bar(NOW+60_000,100.4,102.1,100.3,102.)])
        self.assertEqual(len(state['trades']),1)
        t=state['trades'][0]
        expected=.6*(t['realized_legs'][0]['price']-t['entry'])/t['risk']+.4*(t['exit']-t['entry'])/t['risk']
        self.assertAlmostEqual(t['gross_r'],expected)
        expected_fees=(t['entry']*cfg.maker_fee+(.6*t['realized_legs'][0]['price']+.4*t['exit'])*cfg.taker_fee)/t['risk']
        self.assertAlmostEqual(t['fees_r'],expected_fees)

    def test_stop_first_when_same_minute_touches_stop_and_target(self):
        snap,cfg,ctx,a,p=fixture()
        state=book(p,cfg)
        advance(state,cfg,[bar(NOW,100.5,p['tp2']+.1,p['stop']-.1,100.4)])
        self.assertEqual(len(state['trades']),1)
        self.assertLess(state['trades'][0]['net_r'],0)
        self.assertTrue(state['trades'][0]['ambiguous_ohlc'])

    def test_tp1_does_not_close_or_label_whole_trade_early(self):
        snap,cfg,ctx,a,p=fixture()
        state=book(p,cfg)
        advance(state,cfg,[bar(NOW,100.5,100.55,100.22,100.4),
            bar(NOW+60_000,100.4,p['tp1']+.01,100.3,p['tp1'])])
        self.assertTrue(state['active']['tp1_hit'])
        self.assertFalse(state['trades'])
        self.assertAlmostEqual(state['active']['remaining'],.2)
        self.assertEqual(state['active']['stop'],p['stop'])
        self.assertAlmostEqual(state['active']['pending_stop']['price'],100.25)

    def test_target_before_entry_cancels_using_high_not_report_close(self):
        snap,cfg,ctx,a,p=fixture()
        state=book(p,cfg)
        advance(state,cfg,[bar(NOW,100.5,p['tp1']+.02,100.4,100.5)])
        self.assertFalse(e.pending_plans(state));self.assertFalse(state['trades'])
        self.assertEqual(state['orders'][0]['reason'],'TARGET_REACHED_WITHOUT_FILL')

    def test_minute_gap_freezes_lifecycle_checkpoint(self):
        snap,cfg,ctx,a,p=fixture()
        state=book(p,cfg);before=copy.deepcopy(state)
        snap.now=NOW+180_000;snap.ticker_ts=snap.now
        snap.candles['1m']=[bar(NOW),bar(NOW+120_000)]
        result=e.run_cycle(state,snap,cfg)
        self.assertEqual(result['action'],'DATA_HOLD')
        self.assertEqual(state,before)


class FreshnessAndPortfolio(unittest.TestCase):
    def test_half_r_rejection_spends_zone_but_same_bar_order_is_not_invented(self):
        zone={'low':100.,'high':100.2,'known_ts':NOW}
        first=bar(NOW,100.3,100.5,100.1,100.3)
        self.assertFalse(zones.first_test(zone,[first],1,100.2,.4,NOW+60_000)['mitigated'])
        second=bar(NOW+60_000,100.3,100.45,100.3,100.4)
        self.assertTrue(zones.first_test(zone,[first,second],1,100.2,.4,NOW+120_000)['mitigated'])

    def test_catchup_current_quote_at_quarter_r_and_wait_after(self):
        snap=snapshot();snap.now=NOW+15*60_000
        snap.candles['1m']=[bar(NOW+i*60_000,100.03,100.06,99.99 if i==3 else 100.01,100.04) for i in range(15)]
        snap.bid=100.03;snap.ask=100.05;snap.price=100.04
        yes=setup_scanner.catch_up(snap,100.,99.8,1,NOW,.01)
        self.assertTrue(yes['allowed'])
        snap.ask=100.061
        self.assertEqual(setup_scanner.catch_up(snap,100.,99.8,1,NOW,.01)['status'],'WAIT_RETEST')
        snap.candles['1m'].pop(5);snap.ask=100.05
        self.assertFalse(setup_scanner.catch_up(snap,100.,99.8,1,NOW,.01)['allowed'])

    def test_open_position_and_pending_book_lock_direction(self):
        import portfolio_arbitration
        snap,cfg,ctx,a,p=fixture()
        self.assertFalse(portfolio_arbitration.directional_lock([], [p], -1,snap,cfg,e)['allowed'])
        self.assertEqual(portfolio_arbitration.directional_lock([p],[], -1,snap,cfg,e)['reason'],'PENDING_DIRECTION_LOCK')

    def test_near_zones_merge_and_recheck_wider_stop(self):
        snap,cfg,ctx,a,p=fixture()
        second=copy.deepcopy(p)
        second.update(id='two',event_key='two',setup='PULLBACK_CONTINUATION')
        second['entry_zone']['low']+=.03;second['entry_zone']['high']+=.03
        second['entry']+=.03
        merged=policy.merge_plans([p,second],snap,cfg,e)
        self.assertEqual(len(merged),1)
        self.assertEqual(merged[0]['score'],100)
        self.assertEqual(merged[0]['rating'],'A+')
        self.assertIn('two',merged[0]['parent_keys'])
        self.assertLessEqual(merged[0]['risk'],cfg.max_stop_absolute)


class ReportingAndRecovery(unittest.TestCase):
    def test_four_report_blocks_and_final_wr_not_tp1_hit_rate(self):
        snap,cfg,ctx,a,p=fixture();state=book(p,cfg)
        e.bind_order_numbers(state)
        result={'action':'PLACE_LIMIT','events':[],'analysis':{},'selected_plans':[p]}
        text=e.build_message(state,snap,result,cfg)
        for value in ('РИНКОВИЙ КОНТЕКСТ','НОВИЙ СИГНАЛ','СТАТУС УГОД','СТАТИСТИКА BZU','Limit 1 (50%)','TP1 (60% + Б/У)','0.5 робочого обсягу'):
            self.assertIn(value,text)
        self.assertNotIn('75%',text)
        self.assertIn('WR —',text)

    def test_telegram_sent_before_heavy_files_with_recoverable_checkpoint(self):
        snap,cfg,ctx,a,p=fixture();state=e.new_state(cfg)
        calls=[]
        real_write=e.atomic_write
        with tempfile.TemporaryDirectory() as tmp:
            statepath=Path(tmp)/'state.json';journal=Path(tmp)/'journal.json'
            def write(path,data):
                calls.append(Path(path).name);real_write(path,data)
            def send(url,payload=None,**kwargs):
                calls.append('TELEGRAM');return {'ok':True}
            with patch.object(core,'STATE_FILE',str(statepath)),patch.object(core,'JOURNAL_FILE',str(journal)), \
                 patch.object(core,'TELEGRAM_TOKEN','TEST'),patch.object(core,'TELEGRAM_CHAT_ID','123'), \
                 patch.object(e,'resolve_live_config',side_effect=lambda c:c), \
                 patch.object(e,'collect_snapshot',return_value=snap), \
                 patch.object(e,'run_cycle',return_value={'action':'WATCH','events':[],'analysis':{}}), \
                 patch.object(e,'build_message',return_value='Поточний звіт'), \
                 patch.object(e,'atomic_write',side_effect=write),patch.object(e,'http_json',side_effect=send), \
                 patch.object(e.research_evidence,'record',side_effect=lambda *args:calls.append('ARCHIVE') or {}), \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(e.run_live(core,notify=True),0)
            self.assertLess(calls.index('notification_checkpoint_v12.json'),calls.index('TELEGRAM'))
            self.assertLess(calls.index('TELEGRAM'),calls.index('ARCHIVE'))
            self.assertLess(calls.index('TELEGRAM'),calls.index('state.json'))
            self.assertFalse((Path(tmp)/'notification_checkpoint_v12.json').exists())

    def test_workflow_has_only_15m_schedule_and_exact_version(self):
        text=(e.ROOT/'.github/workflows/main.yml').read_text()
        self.assertIn("cron: '*/15 * * * *'",text)
        self.assertNotIn("cron: '*/5",text)
        self.assertIn(e.VERSION,text)
        self.assertIn('notification_checkpoint_v12.json',text)
        self.assertEqual(text,(e.ROOT/'WORKFLOW_main.yml').read_text())


if __name__=='__main__': unittest.main()
