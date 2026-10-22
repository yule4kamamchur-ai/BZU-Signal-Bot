"""v12.6 behavioral guarantees; synthetic paths are not profitability evidence."""
import copy
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import bot_oneshot as core
import full_limit_engine as engine
import lifecycle_forecast as model
import limit_structure
import research_evidence as evidence
import research_history
import research_v12_6 as research
import research_validate
import signal_quality as quality
import trade_dependencies as dependencies
from test_full_limit_engine import NOW,candle,snapshot,order,analysis
from test_signal_quality import quality_snapshot,observations,confirmed_features


class ContinuousDecisionTests(unittest.TestCase):
    def test_zero_one_and_many_observations_never_apply_a_wr_veto(self):
        cfg,s,ctx,p=quality_snapshot()
        for rows in ([],observations(1,0),observations(40,0),observations(100,90,win_r=.01)):
            q=quality.assess(p,s,ctx,cfg,engine,rows)
            self.assertTrue(q['accepted'],q)
            self.assertFalse(q['win_rate_gate_enabled'])
            self.assertFalse(q['probability_validated'])

    def test_first_resolved_episode_already_changes_ranking(self):
        up=quality.empirical(confirmed_features(),observations(1,1),NOW)
        down=quality.empirical(confirmed_features(),observations(1,0),NOW)
        self.assertGreater(up['ranking_adjustment'],0)
        self.assertLess(down['ranking_adjustment'],0)
        self.assertLess(up['learning_weight'],.1)

    def test_future_labels_cannot_improve_a_live_decision(self):
        rows=observations(1,1);rows[0]['closed_ts']=NOW+1
        self.assertEqual(quality.empirical(confirmed_features(),rows,NOW)['observations'],0)

    def test_structural_risk_veto_stays_active(self):
        cfg,s,ctx,p=quality_snapshot()
        with patch.object(quality,'features',return_value=confirmed_features(shock=True)):
            q=quality.assess(p,s,ctx,cfg,engine,observations(100,100))
        self.assertFalse(q['accepted']);self.assertEqual(q['reason'],'SHOCK_STILL_ACTIVE')


class DependencyTests(unittest.TestCase):
    def trade(self,ident,entry=100.,net=1.,start=NOW):
        return {**order(),'id':ident,'entry':entry,'risk':1.,'filled_ts':start,
                'placed_ts':start,'closed_ts':start+900000,'net_r':net}

    def test_correlated_winner_and_loser_are_one_zero_result_episode(self):
        rows=[self.trade('a',100,1),self.trade('b',100.1,-1)]
        result=dependencies.summary(rows)
        self.assertEqual(result['raw_trades'],2)
        self.assertEqual(result['dependency_episodes'],1)
        self.assertEqual(result['cluster_expectancy_r'],0.)
        self.assertEqual(result['cluster_win_rate'],0.)
        self.assertFalse(result['independence_guaranteed'])

    def test_different_time_or_direction_is_not_a_same_thesis_duplicate(self):
        first=self.trade('a');later=self.trade('b',start=NOW+1800000)
        opposite=self.trade('c');opposite['side']=-1
        self.assertFalse(dependencies.related(first,later))
        self.assertFalse(dependencies.related(first,opposite))

    def test_transitive_clusters_are_order_independent(self):
        rows=[self.trade('a',100),self.trade('b',100.4),self.trade('c',100.8)]
        self.assertEqual(dependencies.summary(rows),dependencies.summary(list(reversed(rows))))
        self.assertEqual(dependencies.summary(rows)['dependency_episodes'],1)

    def test_close_second_entry_is_not_presented_as_additional_diversification(self):
        cfg=engine.Config();state=engine.new_state(cfg)
        state['pending']=order();state['pending']['id']='first'
        second=order();second.update(id='second',event_key='second',entry=100.4)
        with patch.object(engine,'analyze',return_value=analysis([second])):
            result=engine.run_cycle(state,snapshot(price=101.),cfg)
        self.assertFalse(result['selected_plans'])
        self.assertEqual(engine.occupied_slots(state),0)
        eligible=engine.entry_priority(state,NOW,101.,cfg)
        self.assertEqual(len(eligible),1)
        self.assertEqual(len(engine.pending_plans(state)),1)


class FullLifecycleTests(unittest.TestCase):
    def plan(self):
        p=order();p.update(intraday=False,max_hold_minutes=30,effective_ts=NOW,
                          partials={'TP1':.1,'TP2':.9},tp1=101.5,tp=101.5,tp2=104.,
                          expires_ts=NOW+900000)
        return p

    def test_tp1_without_a_following_review_does_not_invent_a_profitable_exit(self):
        p=self.plan();rows=[candle(NOW,100.2,100.3,99.98,100.1),
            candle(NOW+180000,100.1,101.6,100.1,101.5),
            candle(NOW+360000,101.5,101.6,98.9,99.)]
        out=model.simulate(p,rows,engine.Config(),engine,review_ms=86400000)
        self.assertTrue(out['filled']);self.assertTrue(out['tp1_hit'])
        self.assertLess(out['net_r'],0.)
        self.assertEqual(out['close_reason'],'STOP')
        self.assertEqual(len(out['realized_legs']),0)
        self.assertGreater(out['fees_r'],0.)
        self.assertLess(out['stress_net_r'],out['net_r'])

    def test_full_exit_result_and_fees_match_hand_computation(self):
        p=self.plan();p.update(partials={'TP1':.65,'TP2':.35},tp1=102.,tp=102.)
        rows=[candle(NOW,100.2,100.3,99.98,100.1),
            candle(NOW+180000,100.1,102.1,100.1,101.8),
            candle(NOW+360000,101.8,101.9,98.9,99.)]
        out=model.simulate(p,rows,engine.Config(),engine,review_ms=300000)
        exit_tp=101.8*(1-.0002)
        gross=exit_tp-100
        fees=100*.0002+exit_tp*.0005
        carry=100*.0002*(6/1440)
        self.assertAlmostEqual(out['net_r'],gross-fees-carry,places=10)

    def test_exact_touch_does_not_assume_a_fill(self):
        p=self.plan();p['expires_ts']=NOW+180000
        rows=[candle(NOW,100.2,100.3,100.,100.1)]
        out=model.simulate(p,rows,engine.Config(),engine)
        self.assertEqual(out['status'],'UNFILLED');self.assertFalse(out['filled'])

    def test_same_bar_stop_and_target_is_a_loss(self):
        p=self.plan()
        out=model.simulate(p,[candle(NOW,100.2,105,98,104)],engine.Config(),engine)
        self.assertTrue(out['ambiguous_ohlc']);self.assertLess(out['net_r'],0)

    def test_unfinished_lifecycle_is_censored(self):
        out=model.simulate(self.plan(),[candle(NOW,100.2,100.3,99.98,100.1)],engine.Config(),engine)
        self.assertEqual(out['status'],'CENSORED')

    def test_forecast_starts_at_current_price_and_reports_fill_separately(self):
        s=snapshot(price=102.)
        s.candles['3m']=[candle(NOW-(60-i)*180000,100,100.02,99.98,100) for i in range(60)]
        p=self.plan();p['expires_ts']=NOW+360000
        result=model.forecast(p,s,engine.Config(),engine,paths=12)
        self.assertEqual(result['status'],'MODEL_ONLY')
        self.assertEqual(result['fill_probability'],0.)
        self.assertIsNone(result['net_win_probability_given_fill'])
        self.assertEqual(result['expectancy_r_per_placed'],0.)
        self.assertFalse(result['calibrated'])
        before=copy.deepcopy(result)
        s.candles['3m']+=[candle(NOW,102,106,98,105),candle(NOW-180000,102,106,98,105,False)]
        self.assertEqual(model.forecast(p,s,engine.Config(),engine,paths=12),before)

    def test_model_does_not_rewrite_existing_order_cost_contract(self):
        p=self.plan();p.update(include_commission=False)
        before=copy.deepcopy(p)
        model.forecast(p,snapshot(),engine.Config(),engine,paths=4)
        self.assertEqual(p,before)


class ZoneLifecycleTests(unittest.TestCase):
    def zone(self):
        return core.Zone('SUPPLY_OB','SHORT',102,102.5,NOW-10*900000,'15m',2.)

    def test_rejection_wick_keeps_a_strong_barrier(self):
        z=self.zone();rows=[candle(NOW-900000,101.9,103,101.7,101.9)]
        life=limit_structure.zone_lifecycle(z,rows,'15m',NOW,.5,.01)
        self.assertTrue(life['hard_barrier']);self.assertEqual(life['penetration_fraction'],1.)
        self.assertNotEqual(life['state'],'BROKEN_ACCEPTED')

    def test_two_closes_accepting_beyond_zone_remove_the_barrier(self):
        rows=[candle(NOW-1800000,102.4,102.8,102.3,102.7),
              candle(NOW-900000,102.7,102.9,102.6,102.8)]
        life=limit_structure.zone_lifecycle(self.zone(),rows,'15m',NOW,1.,.01)
        self.assertEqual(life['state'],'BROKEN_ACCEPTED');self.assertFalse(life['hard_barrier'])

    def test_age_alone_does_not_dissolve_strong_supply(self):
        z=self.zone();z.created_ts=NOW-80*900000
        life=limit_structure.zone_lifecycle(z,[],'15m',NOW,1.,.01)
        self.assertTrue(life['hard_barrier']);self.assertEqual(life['state'],'AGED_UNTESTED')

    def test_strong_gap_requires_displacement_and_survives_a_rejection_wick(self):
        z=core.Zone('FVG_BEAR','SHORT',102,103,NOW-4*900000,'15m',1.)
        formation=candle(z.created_ts,104,104.1,101.7,101.8)
        rejection=candle(NOW-900000,101.8,103.2,101.7,101.9)
        life=limit_structure.zone_lifecycle(z,[formation,rejection],'15m',NOW,1.,.01)
        self.assertTrue(life['hard_barrier'])
        quiet=replace(formation,open=102.,high=102.1,low=101.7,close=101.8)
        self.assertFalse(limit_structure.zone_lifecycle(z,[quiet,rejection],'15m',NOW,1.,.01)['hard_barrier'])

    def test_entry_inside_opposing_supply_is_not_treated_as_clear_runway(self):
        from test_v12_4_3_geometry import MinimumDistanceTests
        cfg,ctx,a,s=MinimumDistanceTests().fixture('LONG')
        targets=[{'kind':'SUPPLY_OB','level':a.level+3,'distance':3,'hard_barrier':True,
                  'entry_inside_zone':True}]
        with patch.object(engine,'full_targets',return_value=targets):
            p,why=engine.full_plan(a,ctx,s,cfg,{})
        self.assertIsNone(p);self.assertEqual(why,'ENTRY_INSIDE_STRONG_OPPOSING_ZONE')

    def test_future_acceptance_cannot_change_zone_state(self):
        rows=[candle(NOW,102.6,104,102,104),candle(NOW-900000,102.6,104,102,104,False)]
        life=limit_structure.zone_lifecycle(self.zone(),rows,'15m',NOW,1.,.01)
        self.assertTrue(life['hard_barrier'])

    def test_secondary_take_profits_do_not_jump_over_a_nearby_strong_zone(self):
        from test_v12_4_3_geometry import MinimumDistanceTests
        cfg,ctx,a,s=MinimumDistanceTests().fixture('LONG')
        risk=engine.stop_geometry(a,ctx,cfg)[2]
        buffer=max(.15*ctx['atr3'],2*cfg.tick)
        targets=[{'kind':'far_liquidity','level':a.level+2*risk+buffer,'distance':2*risk+buffer,'hard_barrier':False},
                 {'kind':'SUPPLY_OB','level':a.level+2.15*risk,'distance':2.15*risk,'hard_barrier':True},
                 {'kind':'farther','level':a.level+4*risk,'distance':4*risk,'hard_barrier':False}]
        with patch.object(engine,'full_targets',return_value=targets):
            p,why=engine.full_plan(a,ctx,s,cfg,{})
        self.assertIsNotNone(p,why)
        self.assertLessEqual(p['tp2'],targets[1]['level']-buffer+cfg.tick)
        self.assertLessEqual(p['tp3'],targets[1]['level']-buffer+cfg.tick)


class EvidenceTests(unittest.TestCase):
    def history_bundle(self):
        raw=[NOW-180000,100,101,99,100,100,'0','0','1']
        data={'instrument':'BZ-USDT-SWAP','tick':.01,'start_ts':NOW-86400000,'end_ts':NOW,
              'candles':{'3m':[raw]},'smt_candles_15m':[]}
        requests=[]
        for url,body in [
            ('https://www.okx.com/api/v5/public/instruments?instId=BZ-USDT-SWAP',
             {'code':'0','data':[{'instId':'BZ-USDT-SWAP','tickSz':'0.01'}]}),
            ('https://www.okx.com/api/v5/market/history-candles?instId=BZ-USDT-SWAP&bar=3m',
             {'code':'0','data':[raw]})]:
            requests.append({'url':url,'response':copy.deepcopy(body),'received_ts':NOW,
                             'response_sha256':evidence.digest(body)})
        return evidence.seal({'schema':research_history.SCHEMA,'source':'OKX_PUBLIC_HTTP_HISTORY_COLLECTOR',
                              'data':data,'requests':requests})

    def test_downloaded_history_has_matching_http_response_for_every_bar(self):
        result=research_history.verify(self.history_bundle(),engine)
        self.assertFalse(result['observed_live_quotes'])
        self.assertEqual(result['requests'],2)

    def test_resealing_changed_history_cannot_bypass_source_lineage(self):
        bundle=self.history_bundle();bundle['data']['candles']['3m'][0][4]=100.5
        evidence.seal(bundle)
        with self.assertRaisesRegex(ValueError,'matching recorded source'):
            research_history.verify(bundle,engine)

    def test_reconstructed_history_cannot_claim_actual_observed_quotes(self):
        bundle=self.history_bundle();bundle['data']['quotes']=[{'now':NOW}]
        evidence.seal(bundle)
        with self.assertRaisesRegex(ValueError,'must not claim observed live quotes'):
            research_history.verify(bundle,engine)

    def attributed(self,now=NOW):
        s=snapshot(now,price=103.)
        s.metadata={'price_source':'OKX_TICKER','research_provenance':{
            'origin':evidence.ORIGIN,'instrument':s.instrument,'observed_ts':s.now}}
        return s

    def test_unattributed_or_offline_snapshot_never_creates_clean_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'clean.json'
            result=evidence.record(p,snapshot(),engine.Config(),engine)
            self.assertEqual(result['status'],'NOT_RECORDED_UNVERIFIED_SOURCE')
            self.assertFalse(p.exists())

    def test_source_hash_and_first_seen_are_verified(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'clean.json';s=self.attributed()
            evidence.record(p,s,engine.Config(),engine)
            data=engine.read_json(p)
            self.assertEqual(evidence.verify(data,engine)['quotes'],1)
            first=data['candles']['3m'][0][0]
            self.assertEqual(data['first_seen']['3m'][str(first)]['observed_ts'],NOW)
            data['candles']['3m'][0][4]+=1
            with self.assertRaisesRegex(ValueError,'checksum'):
                evidence.verify(data,engine)

    def test_revisions_are_available_only_from_their_observation_time(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'clean.json';s=self.attributed()
            evidence.record(p,s,engine.Config(),engine)
            later=copy.deepcopy(s);later.now+=180000;later.ticker_ts=later.now
            later.metadata['research_provenance']['observed_ts']=later.now
            changed=later.candles['3m'][-2]
            later.candles['3m'][-2]=replace(changed,high=104.)
            evidence.record(p,later,engine.Config(),engine)
            data=engine.read_json(p);samples=list(research_validate.snapshots(evidence.dataset(data,engine)))
            old=next(c for c in samples[0].candles['3m'] if c.ts==changed.ts)
            new=next(c for c in samples[1].candles['3m'] if c.ts==changed.ts)
            self.assertEqual(old.high,changed.high);self.assertEqual(new.high,104.)
            self.assertEqual(data['candles']['3m'][-2][2],changed.high)

    def test_legacy_market_archive_is_never_auto_certified(self):
        with self.assertRaisesRegex(ValueError,'Unverified'):
            evidence.verify({'schema':'closed_market_evidence_v1'},engine)

    def test_smt_peer_is_preserved_and_unavailable_at_earlier_quotes(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'clean.json';s=self.attributed()
            evidence.record(p,s,engine.Config(),engine)
            later=copy.deepcopy(s);later.now+=900000;later.ticker_ts=later.now
            later.metadata['research_provenance']['observed_ts']=later.now
            later.metadata['smt_asset_id']='CL-USDT-SWAP'
            later.smt_candles=[candle(NOW-900000)]
            evidence.record(p,later,engine.Config(),engine)
            samples=list(research_validate.snapshots(evidence.dataset(engine.read_json(p),engine)))
            self.assertFalse(samples[0].smt_candles)
            self.assertEqual(len(samples[1].smt_candles),1)

    def test_same_or_older_scan_does_not_rewrite_evidence(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'clean.json';s=self.attributed()
            evidence.record(p,s,engine.Config(),engine);before=p.read_bytes()
            s.price=999
            self.assertEqual(evidence.record(p,s,engine.Config(),engine)['status'],'UNCHANGED')
            self.assertEqual(p.read_bytes(),before)

    def test_empty_clean_archive_does_not_invent_validation_results(self):
        result=research.validate(evidence.empty('BZ-USDT-SWAP'))
        self.assertEqual(result['status'],'COLLECTING_CLEAN_EVIDENCE')
        self.assertFalse(result['win_rate_entry_gate_enabled']);self.assertFalse(result['live_entry_blocked'])

    def test_short_research_history_does_not_block_live_trading(self):
        result=research.boundaries(NOW,NOW+4*86400000,engine.Config())
        self.assertEqual(result['status'],'INSUFFICIENT_CHRONOLOGICAL_SPAN')
        self.assertFalse(result['live_entry_blocked'])

    def test_validation_and_holdout_freeze_only_training_labels(self):
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder)/'clean.json'
            evidence.record(p,self.attributed(),engine.Config(),engine)
            evidence.record(p,self.attributed(NOW+40*86400000),engine.Config(),engine)
            archive=engine.read_json(p);calls=[]
            def evaluate(module,cfg,samples,begin,end,observations=None,freeze=False):
                calls.append((begin,end,copy.deepcopy(observations),freeze))
                state=engine.new_state(cfg)
                if observations is None:
                    state['quality']={'observations':[{'id':'past','decision_ts':begin,'closed_ts':end-1},
                                                       {'id':'future','decision_ts':begin,'closed_ts':end+1}]}
                return {'trades':[]},state
            with patch.object(research_validate,'audit_data',return_value={}), \
                 patch.object(research_validate,'snapshots',return_value=[]):
                result=research.validate(archive,evaluator=evaluate)
            self.assertEqual(result['status'],'HELD_OUT_EVALUATION_COMPLETE')
            self.assertEqual([r['id'] for r in calls[1][2]],['past'])
            self.assertEqual(calls[1][2],calls[2][2])
            self.assertTrue(calls[1][3]);self.assertTrue(calls[2][3])
            self.assertLess(calls[0][1],calls[1][0]);self.assertLess(calls[1][1],calls[2][0])
            self.assertFalse(result['holdout_used_for_learning'])

    def test_calibration_uses_placement_prediction_not_a_later_updated_forecast(self):
        state=engine.new_state(engine.Config())
        state['orders']=[{**order(),'status':'FILLED',
            'decision_forecast':{'status':'MODEL_ONLY','fill_probability':.2,
                'net_win_probability_given_fill':.3,'expectancy_r_given_fill':.5},
            'outcome_forecast':{'status':'MODEL_ONLY','fill_probability':1.,'net_win_probability_given_fill':1.}}]
        trade={**order(),'net_r':1.,'closed_ts':NOW+900000}
        stats=research.summarize({'trades':[trade]},state)['forecast_evaluation']
        self.assertAlmostEqual(stats['fill_brier'],.64)
        self.assertAlmostEqual(stats['full_net_win_brier'],.49)


class MigrationCostTests(unittest.TestCase):
    def test_legacy_trade_keeps_gross_cost_contract_after_upgrade(self):
        p=order();p.update(include_commission=False,version='full-ict-v12.5.0-causal-early-limits')
        state=engine.new_state(engine.Config());state['active']=p
        engine.close_trade(state,p,101.,NOW,'STOP',replace(engine.Config(),include_commission=True))
        self.assertEqual(state['trades'][0]['fees_r'],0.)
        self.assertEqual(state['trades'][0]['net_r'],1.)
        self.assertEqual(state['trades'][0]['version'],'full-ict-v12.5.0-causal-early-limits')

    def test_new_trade_cost_rates_are_frozen_even_if_runtime_rates_change(self):
        p=order();p.update(include_commission=True,entry_fee_rate=.001,exit_fee_rate=.002)
        state=engine.new_state(engine.Config());state['active']=p
        engine.close_trade(state,p,101.,NOW,'STOP',replace(engine.Config(),maker_fee=.01,taker_fee=.05))
        self.assertAlmostEqual(state['trades'][0]['fees_r'],.1+.202)


if __name__=='__main__':
    unittest.main()
