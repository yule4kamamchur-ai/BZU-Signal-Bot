"""Quality, causal sampling, shadow accounting and held-out research tests.

Synthetic fixtures verify behavior; their wins are never market validation.
"""
import copy
import io
import json
import math
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import Mock, patch

import bot_oneshot as core
import full_limit_engine as engine
import research_validate as research
import signal_quality as quality
from test_full_limit_engine import NOW, analysis, candle, full_fixture, order, snapshot


def confirmed_features(setup='SWEEP_RECLAIM', side=1, **changes):
    family,kind=quality.CONTRACTS[setup]
    f={'setup':setup,'family':family,'kind':kind,'side':side,'regime':'TRANSITION',
       'alignment':'neutral','side3':side,'side15':0,'shift3':False,'shift15':False,
       'impulse':True,'acceptance':False,'range_position':.2 if side==1 else .8,
       'shock':False,'distance_atr':.5,'cost_fraction':.03,'as_of_ts':NOW,'strategy':quality.STRATEGY}
    if kind in ('breakout','continuation'):
        f['side15']=side
    f.update(changes)
    return f


def observations(n=40, wins=30, setup='SWEEP_RECLAIM', win_r=1., loss_r=-1.):
    return [{'id':str(i),'strategy':quality.STRATEGY,'qualified':True,
             'features':confirmed_features(setup),
             'decision_ts':NOW-(n+1-i)*1_800_000,'closed_ts':NOW-(n+1-i)*1_800_000+180_000,
             'net_r':win_r if i<wins else loss_r} for i in range(n)]


def quality_snapshot():
    s=snapshot(price=101.25)
    rows=[]
    for i,c in enumerate(s.candles['3m']):
        close=100+.008*i+.10*math.sin(i*math.pi/3)
        rows.append(candle(c.ts,close-.02,close+.06,close-.08,close))
    rows[-1]=candle(rows[-1].ts,100.94,101.28,100.90,101.25)
    s.candles['3m']=rows
    ctx={'structure3m':core.structure_snapshot(rows,60,2),
         'structure15':core.structure_snapshot(s.candles['15m'],60,2),
         'atr3':engine.atr(rows),'atr15':engine.atr(s.candles['15m']),
         'regime':'TRANSITION','regime_memory':{}}
    p=order();p.update(setup='SWEEP_RECLAIM',setup_type='SWEEP_RECLAIM',
                       entry=100.70,stop=99.70,initial_stop=99.70,risk=1.,tp=102.70,
                       invalidation=99.70,gross_rr=2.,costs_r=.08,
                       canonical_setup_family='LIQUIDITY_REVERSAL',htf_1h=0,htf_4h=0)
    return replace(engine.Config(),core=core),s,ctx,p


class ConfirmationTests(unittest.TestCase):
    def test_every_original_setup_has_an_executable_confirmation_contract(self):
        self.assertEqual(set(quality.CONTRACTS),set(core.DETECTED_SETUP_TYPES))
        self.assertEqual(len(quality.CONTRACTS),24)
        for setup in quality.CONTRACTS:
            for side in (-1,1):
                with self.subTest(setup=setup,side=side):
                    self.assertEqual(quality.structural_reason(confirmed_features(setup,side)),'CONFIRMED')

    def test_actual_closed_bars_support_positive_bootstrap_without_invented_probability(self):
        cfg,s,ctx,p=quality_snapshot()
        self.assertEqual(ctx['structure3m']['direction'],'LONG')
        q=quality.assess(p,s,ctx,cfg,engine,[])
        self.assertTrue(q['accepted'],q)
        self.assertTrue(q['features']['impulse'])
        self.assertIsNone(q['statistics']['estimate'])
        self.assertFalse(q['probability_validated'])
        self.assertEqual(q['status'],'BOOTSTRAP_NOT_VALIDATED')

    def test_full_original_registry_can_place_confirmed_signals_without_detector_mocks(self):
        # Independent synthetic timeframe fixtures, not historical OHLC or a
        # profitability experiment. Both confirmations and real planners run.
        cfg,_,_,s=full_fixture()
        for i,c in enumerate(s.candles['3m'][-30:]):
            close=70+.009*(29-i)+.08*math.sin(i*math.pi/3)
            s.candles['3m'][-30+i]=candle(c.ts,close+.025,close+.085,close-.06,close)
        c=s.candles['3m'][-1];s.candles['3m'][-1]=candle(c.ts,70.22,70.25,69.94,70)
        for i,c in enumerate(s.candles['15m'][-2:]):
            close=70.18 if i==0 else 70.
            s.candles['15m'][-2+i]=candle(c.ts,close+.18,close+.22,69.5,close)
        state=engine.new_state(cfg);result=engine.run_cycle(state,s,cfg)
        self.assertEqual(result['action'],'PLACE_LIMIT')
        self.assertEqual(len(result['analysis']['detectors']),24)
        self.assertTrue(all(d['status']!='ERROR' for d in result['analysis']['detectors']))
        self.assertEqual(len(engine.open_plans(state)),2)
        for plan in engine.open_plans(state):
            self.assertTrue(plan['quality']['accepted']);self.assertIsNone(plan['probability'])

    def test_counterdirection_has_to_confirm_both_timeframes(self):
        f=confirmed_features(side15=-1,shift3=True)
        self.assertEqual(quality.structural_reason(f),'REVERSAL_NOT_CONFIRMED')
        f['shift15']=True
        self.assertEqual(quality.structural_reason(f),'CONFIRMED')
        f.update(alignment='against',shift15=False,side15=1)
        self.assertEqual(quality.structural_reason(f),'HTF_REVERSAL_NOT_CONFIRMED')

    def test_continuations_require_15m_acceptance_and_cannot_fight_both_htf(self):
        f=confirmed_features('BREAKOUT_RETEST',side15=0)
        self.assertEqual(quality.structural_reason(f),'NO_15M_ACCEPTANCE')
        f.update(acceptance=True)
        self.assertEqual(quality.structural_reason(f),'CONFIRMED')
        f.update(alignment='against')
        self.assertEqual(quality.structural_reason(f),'CONTINUATION_AGAINST_HTF')

    def test_missing_control_or_departure_blocks_limit(self):
        self.assertEqual(quality.structural_reason(confirmed_features(side3=0)),'NO_CONFIRMED_3M_CONTROL')
        self.assertEqual(quality.structural_reason(confirmed_features(impulse=False)),'NO_CONFIRMED_DEPARTURE')

    def test_range_interiors_shocks_costs_and_long_return_paths_rejected(self):
        cases=[(confirmed_features('RANGE_EDGE_REVERSAL',range_position=.5),'RANGE_ENTRY_NOT_AT_EDGE'),
               (confirmed_features('TIME_OF_DAY_ADAPTIVE',-1,range_position=.5),'RANGE_ENTRY_NOT_AT_EDGE'),
               (confirmed_features(shock=True),'SHOCK_STILL_ACTIVE'),
               (confirmed_features(cost_fraction=.30),'COSTS_CONSUME_EDGE'),
               (confirmed_features(distance_atr=3.),'RETURN_PATH_TOO_LONG')]
        for f,reason in cases:
            self.assertEqual(quality.structural_reason(f),reason)

    def test_future_and_unconfirmed_candles_do_not_change_features(self):
        cfg,s,ctx,p=quality_snapshot();before=quality.features(p,s,ctx,cfg,engine)
        for tf,step in engine.TF.items():
            s.candles[tf].extend([candle(NOW,50,200,10,180),
                                 candle(NOW-step,50,200,10,180,confirmed=False)])
        self.assertEqual(quality.features(p,s,ctx,cfg,engine),before)

    def test_structural_shift_uses_a_pivot_known_before_crossing(self):
        rows=[candle(NOW+i*180_000,100,101,99,100) for i in range(12)]
        rows[10]=candle(rows[10].ts,100,103,99,102)
        rows[11]=candle(rows[11].ts,102,103,101,102)
        point={'kind':'HIGH','level':101.,'known_ts':rows[10].ts}
        with patch.object(engine,'pivots',return_value=[point]):
            self.assertFalse(quality.shifted(rows,1,engine))
        point['known_ts']=rows[9].ts
        with patch.object(engine,'pivots',return_value=[point]):
            self.assertTrue(quality.shifted(rows,1,engine))

    def test_valid_reversal_survives_higher_score_unconfirmed_neighbor_and_places(self):
        cfg,s,ctx,p=quality_snapshot();_,_,a,_=full_fixture()
        anchors=[];plans={}
        for setup,score in [('BREAKOUT_RETEST',95),('SWEEP_RECLAIM',75)]:
            anchor=copy.deepcopy(a);anchor.setup_type=setup;anchor.id=setup;anchors.append(anchor)
            plan=copy.deepcopy(p);plan.update(id=setup,event_key=setup,setup=setup,setup_type=setup,
                                            canonical_setup_family=quality.CONTRACTS[setup][0],score=score)
            plans[setup]=plan
        detectors=[]
        for i in range(24):
            detector=Mock(return_value=anchors[i] if i<len(anchors) else None)
            detector.__name__='contract_detector_'+str(i);detectors.append(detector)
        state=engine.new_state(cfg)
        with patch.object(core,'DETECTORS',detectors),patch.object(engine,'build_full_context',return_value=ctx), \
             patch.object(engine,'full_plan',side_effect=lambda anchor,*_: (copy.deepcopy(plans[anchor.setup_type]),'ACCEPTED')):
            result=engine.run_cycle(state,s,cfg)
        self.assertEqual(result['action'],'PLACE_LIMIT',result)
        self.assertEqual(state['pending']['setup'],'SWEEP_RECLAIM')
        self.assertEqual(state['pending']['score'],75)  # correlated votes add no score
        self.assertEqual(state['pending']['supporting_setups'],['SWEEP_RECLAIM'])
        self.assertEqual(len(result['analysis']['quality_assessments']),2)
        self.assertEqual(len(state['quality']['pending']),1)
        self.assertEqual(state['quality']['pending'][0]['pending']['setup'],'SWEEP_RECLAIM')
        self.assertEqual(engine.occupied_slots(state),1)


class EmpiricalTests(unittest.TestCase):
    def test_future_wrong_strategy_unqualified_and_old_labels_excluded(self):
        rows=observations(1,1);wrong=[]
        for change in ({'strategy':'old'},{'qualified':False},{'closed_ts':NOW},
                       {'decision_ts':NOW+1,'closed_ts':NOW+2},{'closed_ts':NOW-91*research.DAY}):
            row=copy.deepcopy(rows[0]);row.update(change);wrong.append(row)
        stat=quality.empirical(confirmed_features(),rows+wrong,NOW)
        self.assertEqual(stat['observations'],1)
        self.assertLess(stat['latest_label_ts'],NOW)

    def test_many_correlated_signals_are_one_episode(self):
        rows=observations(100,100)
        for row in rows:
            row.update(decision_ts=NOW-900_000,closed_ts=NOW-180_000)
        stat=quality.empirical(confirmed_features(),rows,NOW)
        self.assertEqual(stat['observations'],1)
        self.assertEqual(stat['wins'],1)

    def test_same_context_family_is_fallback_until_exact_setup_has_enough_labels(self):
        rows=observations(40,30,'FAILED_AUCTION_REJECTION')
        stat=quality.empirical(confirmed_features(),rows,NOW)
        self.assertEqual(stat['scope'],'family');self.assertEqual(stat['observations'],40)
        for row in rows:
            row['features']['alignment']='mixed'
        self.assertEqual(quality.empirical(confirmed_features(),rows,NOW)['observations'],0)

    def test_low_rate_blocks_and_high_rate_with_negative_expectancy_also_blocks(self):
        cfg,s,ctx,p=quality_snapshot()
        for rows,reason in [(observations(40,20),'EMPIRICAL_WIN_RATE_BELOW_TARGET'),
                            (observations(40,32,win_r=.1),'EXPECTANCY_TOO_LOW_AFTER_COSTS')]:
            q=quality.assess(p,s,ctx,cfg,engine,rows)
            self.assertFalse(q['accepted']);self.assertEqual(q['reason'],reason)

    def test_positive_net_cohort_accepts_but_is_not_a_validated_probability(self):
        cfg,s,ctx,p=quality_snapshot();q=quality.assess(p,s,ctx,cfg,engine,observations())
        self.assertTrue(q['accepted'],q)
        self.assertEqual(q['statistics']['observations'],40)
        self.assertGreater(q['statistics']['estimate'],.70)
        self.assertGreater(q['statistics']['expectancy_r'],0)
        self.assertFalse(q['probability_validated'])

    def test_validation_requires_enough_independent_wins_and_positive_net_result(self):
        for wins,n,win_r,expected in [(10,10,1,False),(70,100,1,False),(85,100,1,True),(99,100,.001,False)]:
            trades=[{'net_r':win_r if i<wins else -1} for i in range(n)]
            self.assertEqual(quality.validation_status(trades)['target_confirmed_on_this_period'],expected)
        trades=[{'net_r':1} for _ in range(100)]
        self.assertFalse(quality.validation_status(trades,trades[:2])['target_confirmed_on_this_period'])


class ShadowAndModelTests(unittest.TestCase):
    def setUp(self):
        self.cfg=replace(engine.Config(),core=core);self.state=engine.new_state(self.cfg)
        self.p=order();self.p.update(setup='SWEEP_RECLAIM',gross_rr=2.,costs_r=.08,execution='PAPER_SIGNAL')
        decision={'features':confirmed_features()}
        quality.watch_candidates(self.state,[self.p],{self.p['id']:decision},self.cfg)
        self.state['quality']['last_run_ts']=NOW

    def later(self,stop=False):
        s=snapshot(NOW+900_000,100.8)
        s.candles['3m'][-5:]=([candle(NOW,101,103 if stop else 101.5,98 if stop else 99.9,100.8)]+
                            [candle(NOW+i*180_000,100.8,103,100.5,102.5) for i in range(1,5)])
        return s

    def test_shadow_profit_is_labelled_once_without_touching_portfolio_events_or_wr(self):
        before={k:copy.deepcopy(self.state[k]) for k in ('pending','active','orders','trades','events')}
        s=self.later();quality.advance(self.state,s,self.cfg,engine)
        quality.advance(self.state,s,self.cfg,engine)
        for key,value in before.items():
            self.assertEqual(self.state[key],value)
        rows=self.state['quality']['observations'];self.assertEqual(len(rows),1)
        self.assertGreater(rows[0]['net_r'],0)
        self.assertEqual(engine.occupied_slots(self.state),0)
        self.assertEqual(engine.statistics(self.state['trades'])['trades'],0)

    def test_conservative_same_bar_stop_loss_remains_in_training_labels(self):
        quality.advance(self.state,self.later(stop=True),self.cfg,engine)
        row=self.state['quality']['observations'][0]
        self.assertLess(row['net_r'],0);self.assertTrue(row['conservative_ambiguity'])

    def test_expired_unfilled_orders_are_not_wins(self):
        self.state['quality']['pending'][0]['pending']['expires_ts']=NOW+180_000
        s=snapshot(NOW+900_000);quality.advance(self.state,s,self.cfg,engine)
        self.assertFalse(self.state['quality']['pending'])
        self.assertFalse(self.state['quality']['observations'])

    def test_gaps_discard_unknown_counterfactual_without_inventing_result(self):
        s=self.later();s.candles['3m'].pop(-3)
        quality.advance(self.state,s,self.cfg,engine)
        self.assertFalse(self.state['quality']['pending'])
        self.assertFalse(self.state['quality']['observations'])
        self.assertEqual(len(self.state['quality']['incomplete_intervals']),1)

    def test_held_out_model_does_not_learn_its_test_outcome(self):
        q=self.state['quality'];q['observations']=observations(1,1);q['freeze_observations']=True
        before=copy.deepcopy(q['observations']);quality.advance(self.state,self.later(),self.cfg,engine)
        self.assertEqual(q['observations'],before);self.assertFalse(q['pending'])

    def test_duplicate_candidates_do_not_multiply_observations(self):
        p=copy.deepcopy(self.p);p.update(id='duplicate',setup='FAILED_AUCTION_REJECTION')
        quality.watch_candidates(self.state,[p],{p['id']:{'features':confirmed_features(p['setup'])}},self.cfg)
        self.assertEqual(len(self.state['quality']['pending']),1)

    def test_shadow_capacity_has_no_effect_on_two_real_slots(self):
        candidates=[];decisions={}
        for i in range(60):
            p=copy.deepcopy(self.p);p.update(id='level-'+str(i),entry=110+i)
            candidates.append(p);decisions[p['id']]={'features':confirmed_features()}
        quality.watch_candidates(self.state,candidates,decisions,self.cfg)
        self.assertEqual(len(self.state['quality']['pending']),quality.Settings().maximum_shadow_plans)
        self.assertGreater(self.state['quality']['unobserved_due_capacity'],0)
        self.assertEqual(engine.occupied_slots(self.state),0)

    def test_cost_change_archives_old_model_and_keeps_real_pending_plan(self):
        self.state['pending']=order();old=copy.deepcopy(self.state['pending'])
        changed=replace(self.cfg,maker_fee=.001)
        q=quality.ensure(self.state,changed)
        self.assertFalse(q['observations']);self.assertFalse(q['pending'])
        self.assertEqual(len(self.state['quality_archives']),1);self.assertEqual(self.state['pending'],old)

    def test_unknown_ledger_is_not_silently_deleted(self):
        self.state['quality']['schema']='unknown'
        with self.assertRaises(ValueError):
            quality.ensure(self.state,self.cfg)

    def model(self,rows=None,**changes):
        result={'schema':quality.SCHEMA,'strategy':quality.STRATEGY,'signature':quality.signature(self.cfg),
                'trained_until_ts':NOW-1,'observations':rows if rows is not None else observations(1,1)}
        result.update(changes);return result

    def test_seed_is_idempotent_and_rejects_wrong_costs_and_future_labels(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'model.json';path.write_text(json.dumps(self.model()))
            quality.seed(self.state,self.cfg,path);quality.seed(self.state,self.cfg,path)
            self.assertEqual(len(self.state['quality']['observations']),1)
            for artifact in [self.model(signature='wrong'),self.model(trained_until_ts=NOW-90*research.DAY)]:
                path.write_text(json.dumps(artifact))
                with self.assertRaises(ValueError):
                    quality.seed(self.state,self.cfg,path)

    def test_corrupt_features_settings_and_checksums_cannot_seed_the_model(self):
        rows=observations(1,1);rows[0]['features'].pop('regime')
        artifacts=[self.model(rows),self.model(settings={'target_win_rate':.99}),self.model(artifact_id='wrong')]
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'model.json'
            for artifact in artifacts:
                path.write_text(json.dumps(artifact))
                with self.assertRaises(ValueError):
                    quality.seed(self.state,self.cfg,path)

    def test_rejected_optional_model_does_not_interrupt_live_followup(self):
        cfg=replace(self.cfg,tick=.01);state=engine.new_state(cfg)
        state['pending']=order();state['last_run_ts']=NOW
        response=Mock(json=Mock(return_value={'code':'0','data':[{
            'instId':'BZ-USDT-SWAP','state':'live','tickSz':'0.01'}]}))
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'state.json';engine.atomic_write(path,state)
            with patch.object(core,'STATE_FILE',str(path)),patch.object(core,'JOURNAL_FILE',str(Path(tmp)/'journal.json')), \
                 patch.object(core,'http_get',return_value=response),patch.object(engine,'collect_snapshot',return_value=snapshot(NOW+900_000,price=101)), \
                 patch.object(engine,'analyze',return_value=analysis()),patch.object(quality,'seed',side_effect=ValueError('wrong model')), \
                 redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
                result=engine.run_live(core)
            saved=engine.read_json(path)
        self.assertEqual(result,0)
        self.assertIsNotNone(saved['pending'])
        self.assertEqual(saved['pending']['id'],state['pending']['id'])
        self.assertIn('quality_model_warning',saved)


class ResearchTests(unittest.TestCase):
    def data(self):
        s=snapshot();s.candles['3m'] += [candle(NOW+i*180_000) for i in range(5)]
        s.candles['15m'].append(candle(NOW))
        return {'instrument':s.instrument,'tick':.001,'start_ts':NOW,'end_ts':NOW+900_000,
                'report_delay_ms':180_000,'candles':{tf:[asdict(c) for c in rows] for tf,rows in s.candles.items()}}

    def test_snapshot_uses_only_candles_closed_by_delayed_15m_report(self):
        data=self.data();data['smt_candles_15m']=copy.deepcopy(data['candles']['15m'])
        for tf in engine.TF:
            data['candles'][tf].append(asdict(candle(NOW+2*engine.TF[tf])))
        samples=list(research.snapshots(data));self.assertEqual([s.now for s in samples],[NOW+180_000])
        for s in samples:
            for tf,rows in s.candles.items():
                self.assertTrue(all(c.ts+engine.TF[tf]<=s.now for c in rows))
            self.assertTrue(all(c.ts+900_000<=s.now for c in s.smt_candles))

    def test_history_completeness_is_required_for_research(self):
        data=self.data();self.assertEqual(research.audit_data(data)['3m']['gaps'],0)
        data['candles']['3m'].pop(-3)
        with self.assertRaisesRegex(ValueError,'Incomplete'):
            research.audit_data(data)

    def test_warmup_and_final_coverage_are_checked(self):
        for mutation in ('warmup','end'):
            data=self.data()
            if mutation=='warmup':
                data['candles']['4H']=data['candles']['4H'][-2:]
            else:
                data['end_ts']+=research.DAY
            with self.assertRaises(ValueError):
                research.audit_data(data)

    def test_history_pagination_filters_open_rows_and_moves_older(self):
        def raw(ts,confirm='1'):
            return [str(ts),'100','101','99','100','100','100','100',confirm]
        pages=[{'data':[raw(NOW),raw(NOW-180_000),raw(NOW-360_000,'0')]},
               {'data':[raw(NOW-360_000),raw(NOW-540_000)]}]
        fetch=Mock(side_effect=pages)
        with patch.object(research.time,'sleep'):
            rows=research.fetch_rows('BZ-USDT-SWAP','3m',NOW-540_000,NOW,fetch)
        self.assertEqual([r['ts'] for r in rows],[NOW-540_000,NOW-360_000,NOW-180_000])
        self.assertIn('after='+str(NOW-360_000),fetch.call_args_list[1].args[0])

    def test_pagination_cannot_silently_stall(self):
        fetch=Mock(return_value={'data':[[str(NOW),'100','101','99','100','100','100','100','1']]})
        with self.assertRaisesRegex(ValueError,'pagination'):
            research.fetch_rows('BZ-USDT-SWAP','3m',NOW-900_000,NOW,fetch)

    def test_http_403_is_reported_and_not_bypassed(self):
        error=urllib.error.HTTPError('https://www.okx.com',403,'Forbidden',{},None)
        with patch.object(research.urllib.request,'urlopen',side_effect=error) as request:
            with self.assertRaisesRegex(RuntimeError,'HTTP 403'):
                research.request_json('https://www.okx.com')
        request.assert_called_once()

    def test_real_research_cycle_runs_offline_without_network_or_production_writes(self):
        cfg,_,_,s=full_fixture();samples=[s]
        with patch.object(research.urllib.request,'urlopen',side_effect=AssertionError('network')):
            report,state=research.evaluate(engine,cfg,samples,NOW,NOW)
        self.assertEqual(report['statistics']['trades'],0)
        self.assertFalse(report['target']['target_confirmed_on_this_period'])
        self.assertTrue(state['quality']['pending'])

    def test_frozen_baseline_and_improved_engine_compare_the_same_weak_snapshot(self):
        cfg,_,_,s=full_fixture();baseline=research.baseline_module()
        base_cfg=baseline.Config(**{k:v for k,v in vars(cfg).items() if k!='core'},core=core)
        old,_=research.evaluate(baseline,base_cfg,[s],NOW,NOW)
        new,_=research.evaluate(engine,cfg,[s],NOW,NOW)
        self.assertEqual(old['actions'],{'PLACE_LIMIT':1})
        self.assertEqual(new['actions'],{'WATCH':1})
        self.assertEqual(old['statistics']['trades'],0)
        self.assertEqual(new['statistics']['trades'],0)

    def test_fixed_period_end_liquidation_includes_filled_position_and_costs(self):
        cfg=replace(engine.Config(),core=core)
        first=snapshot(NOW,101);later=snapshot(NOW+900_000,100.8)
        later.candles['3m'][-5:]=([candle(NOW,101,101.2,99.9,100.8)]+
                                [candle(NOW+i*180_000,100.8,100.9,100.7,100.8) for i in range(1,5)])
        with patch.object(engine,'analyze',side_effect=[analysis([order()]),analysis()]):
            report,_=research.evaluate(engine,cfg,[first,later],NOW,NOW+900_000)
        self.assertEqual(report['statistics']['trades'],1)
        t=report['trades'][0];self.assertEqual(t['close_reason'],'EVALUATION_END')
        self.assertLess(t['net_r'],.8);self.assertGreater(t['fees_r'],0)
        self.assertEqual(report['filled_positions_liquidated_at_period_end'],[t['id']])

    def test_three_folds_freeze_test_models_and_exclude_future_training_labels(self):
        data={'start_ts':NOW-90*research.DAY,'end_ts':NOW,'instrument':'BZ-USDT-SWAP','tick':.001}
        calls=[];baseline=Mock(Config=engine.Config)
        def evaluate(module,cfg,samples,begin,end,rows=None,freeze=False):
            calls.append((module,begin,end,copy.deepcopy(rows),freeze))
            report={'trades':[],'statistics':engine.statistics([])}
            if rows is None and module is engine:
                before=observations(1,1)[0];before.update(decision_ts=end-180_000,closed_ts=end-1)
                future=copy.deepcopy(before);future.update(id='future',closed_ts=end+1)
                return report,{'quality':{'observations':[before,future]}}
            return report,{}
        with patch.object(research,'audit_data',return_value={}),patch.object(research,'snapshots',return_value=[snapshot()]), \
             patch.object(research,'baseline_module',return_value=baseline),patch.object(research,'evaluate',side_effect=evaluate), \
             redirect_stdout(io.StringIO()):
            result=research.validate(data)
        self.assertEqual(len(calls),9)
        for i in range(3):
            train,test,old=calls[i*3:i*3+3]
            self.assertTrue(test[4]);self.assertEqual(len(test[3]),1)
            self.assertLess(test[3][0]['closed_ts'],train[2])
            self.assertGreaterEqual(test[1]-train[2],result['folds'][i]['embargo_ms'])
            self.assertEqual((test[1],test[2]),(old[1],old[2]))
        self.assertLess(result['model']['observations'][0]['closed_ts'],result['model']['trained_until_ts'])
        self.assertFalse(result['target_evidence']['target_confirmed_on_this_period'])


if __name__=='__main__':
    unittest.main()
