"""Exact entry zones, opposite-quote gates, uncalibrated forecasts and release checks."""
import copy
import math
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bot_oneshot as core
import full_limit_engine as engine
import price_forecast as model
import setup_scanner as scanner
from test_full_limit_engine import NOW, candle, snapshot, full_fixture


class QuoteAndZoneTests(unittest.TestCase):
    def test_exact_price_match_is_labeled_at_level_not_rounded_zero_percent(self):
        text=scanner.distance_text(103.05,103.05,.001)
        self.assertIn('Ціна на рівні входу 103.05',text)
        self.assertIn('0 тиків',text)
        self.assertNotIn('0.000%',text)
        self.assertTrue(scanner.distance_details(103.05,103.05,.001)['at_level'])

    def test_tiny_nonzero_gap_is_not_mistaken_for_exact_match(self):
        text=scanner.distance_text(103.05001,103.05,.001)
        self.assertIn('<0.001%',text)
        self.assertNotIn('Ціна на рівні',text)
        self.assertGreater(scanner.distance_details(103.05001,103.05,.001)['ticks'],0)

    def test_distance_to_103_07_has_a_price_amount_and_exchange_ticks(self):
        text=scanner.distance_text(103.05,103.07,.001)
        self.assertIn('0.019%',text)
        self.assertIn('0.02 USDT',text)
        self.assertIn('20 тиків',text)

    def test_inside_spread_limit_is_passive_for_both_sides(self):
        for side in (1,-1):
            self.assertTrue(scanner.passive_gate(103.05,side,103.04,103.06,.001)['passive'])
        self.assertFalse(scanner.passive_gate(103.06,1,103.04,103.06,.001)['passive'])
        self.assertFalse(scanner.passive_gate(103.04,-1,103.04,103.06,.001)['passive'])

    def test_wait_instruction_has_the_actual_quote_level_and_current_quote(self):
        text=scanner.wait_text(103.07,1,103.04,103.06,.001)
        self.assertIn('LONG 103.07',text)
        self.assertIn('ASK ≥ 103.071',text)
        self.assertIn('зараз 103.06',text)
        short=scanner.wait_text(103.04,-1,103.04,103.06,.001)
        self.assertIn('BID ≤ 103.039',short)

    def test_planner_can_arm_at_last_trade_price_inside_spread(self):
        for side in ('LONG','SHORT'):
            cfg,ctx,a,s=full_fixture(side)
            s.price=a.level;s.bid=a.level-.01;s.ask=a.level+.01
            p,why=engine.full_plan(a,ctx,s,cfg,{})
            self.assertIsNotNone(p,why)
            self.assertEqual(p['entry'],s.price)
            self.assertIsNone(p['probability'])

    def test_preview_has_same_structural_geometry_and_never_grants_admission(self):
        cfg,ctx,a,s=full_fixture()
        p,why=engine.full_plan(a,ctx,s,cfg,{})
        self.assertIsNotNone(p,why)
        preview=engine.preview_geometry(a,ctx,s,cfg)
        for key in ('entry','stop','risk','tp1','gross_rr'):
            self.assertAlmostEqual(preview[key],p[key])
        self.assertTrue(preview['geometry_preview'])
        a.level=s.ask
        p,why=engine.full_plan(a,ctx,s,cfg,{})
        self.assertIsNone(p);self.assertEqual(why,'LIMIT_WOULD_CROSS_SPREAD')


class ForecastTests(unittest.TestCase):
    def scenario(self, direction=0, ranges=False):
        cfg=engine.Config();s=snapshot();rows=[];price=100.
        for original in s.candles['3m'][-80:]:
            opening=price;price=opening*(1+direction*.01)
            high=max(opening,price)*(1.02 if ranges else 1.001)
            low=min(opening,price)*(0.98 if ranges else .999)
            rows.append(candle(original.ts,opening,high,low,price))
        s.candles['3m']=rows
        p={'side':1,'entry':100.,'initial_stop':99.,'tp1':100.5,
           'setup_timeframe':'15m','max_hold_minutes':30,'intraday':False}
        return cfg,s,p

    def test_model_uses_observed_direction_and_is_never_labeled_calibrated_win_rate(self):
        cfg,up,p=self.scenario(1);_,down,_=self.scenario(-1)
        bullish=model.forecast(p,up,cfg);bearish=model.forecast(p,down,cfg)
        self.assertGreater(bullish['tp1_before_sl'],bearish['tp1_before_sl'])
        self.assertFalse(bullish['calibrated'])
        self.assertIsNone(bullish['empirical_win_rate'])
        self.assertEqual(bullish['target'],'TP1_BEFORE_INITIAL_SL')

    def test_model_can_evaluate_short_with_a_declining_history(self):
        cfg,s,p=self.scenario(-1);p.update(side=-1,initial_stop=101.,tp1=99.5)
        estimate=model.forecast(p,s,cfg)
        self.assertGreater(estimate['tp1_before_sl'],.9)

    def test_both_barriers_in_one_scenario_bar_conservatively_count_as_stop(self):
        cfg,s,p=self.scenario(0,ranges=True)
        estimate=model.forecast(p,s,cfg)
        self.assertEqual(estimate['tp1_paths'],0)
        self.assertEqual(estimate['sl_paths'],model.PATHS)
        self.assertEqual(estimate['ambiguous_paths'],model.PATHS)

    def test_timeout_is_not_counted_as_tp_or_a_win(self):
        cfg,s,p=self.scenario();p.update(initial_stop=90.,tp1=110.)
        estimate=model.forecast(p,s,cfg)
        self.assertEqual(estimate['timeout_paths'],model.PATHS)
        self.assertEqual(estimate['tp1_before_sl'],0.)
        self.assertEqual(estimate['stop_before_tp1'],0.)

    def test_paths_are_repeatable_and_future_or_open_candles_do_not_affect_forecast(self):
        cfg,s,p=self.scenario(1);before=model.forecast(p,s,cfg)
        s.candles['3m'] += [candle(NOW,100,150,50,130),candle(NOW-180000,100,150,50,130,False)]
        self.assertEqual(model.forecast(p,s,cfg),before)
        p['price_forecast']=before
        self.assertIs(model.forecast(p,s,cfg),before)
        self.assertLessEqual(before['last_closed_ts'],s.now)

    def test_missing_history_or_barriers_has_an_explicit_reason_not_a_fake_probability(self):
        cfg,s,p=self.scenario();p.pop('tp1')
        self.assertEqual(model.forecast(p,s,cfg)['status'],'NEEDS_SL_TP')
        p['tp1']=101.;s.candles['3m']=s.candles['3m'][:8]
        result=model.forecast(p,s,cfg)
        self.assertEqual(result['status'],'NEEDS_CLOSED_HISTORY')
        self.assertNotIn('tp1_before_sl',result)

    def test_model_output_and_empirical_estimate_are_kept_separate(self):
        cfg,s,p=self.scenario(1);forecast=model.forecast(p,s,cfg)
        text=engine.expected_line({'observations':0},forecast)
        self.assertIn('цінова модель після входу',text)
        self.assertIn('80 закритих 3m свічок',text)
        self.assertIn('модель ще не перевірена',text)
        self.assertNotIn('н/д',text)
        proven=engine.expected_line({'observations':40,'estimate':.8,'scope':'setup','lower_95':.65},forecast)
        self.assertIn('Оцінка win rate: 80.0%',proven)
        self.assertNotIn('цінова модель',proven)

    def test_actual_cycle_persists_forecast_without_replacing_empirical_probability(self):
        from test_signal_quality import quality_snapshot
        cfg,s,ctx,p=quality_snapshot();state=engine.new_state(cfg)
        result=engine.run_cycle(state,s,cfg)
        self.assertTrue(result['selected_plans'])
        selected=result['selected_plans'][0]
        self.assertEqual(selected['outcome_forecast']['status'],'MODEL_ONLY')
        self.assertIsNone(selected['probability'])
        self.assertFalse(selected['outcome_forecast']['calibrated'])


class ReleaseAndMessageTests(unittest.TestCase):
    def test_message_has_only_requested_icons_and_shows_its_release(self):
        cfg=replace(engine.Config(),core=core);s=snapshot(price=103.05)
        w={'setup':'ACCEPTANCE_RETEST_CONTINUATION','timeframe':'1H','side':1,'level':103.07,
           'readiness_pct':40.,'reason':'LIMIT_WOULD_CROSS_SPREAD','score':60.}
        text=engine.build_message(engine.new_state(cfg),s,{'action':'WATCH','analysis':{'watch':[w]}},cfg)
        self.assertIn(engine.REPORT_RELEASE,text)
        self.assertIn('поточні ASK/BID не дозволяють пасивний ліміт',text)
        self.assertNotIn('комісі',text)
        self.assertIn('💰 Ціна зараз:',text)
        self.assertIn('НА СПОСТЕРЕЖЕННІ — ордер ще не активований',text)
        self.assertIn('готовність умов 40%',text)
        for line in text.splitlines():
            if line and line[0] not in '💰🏆🧩':
                self.assertLess(ord(line[0]),0x2300,line)

    def test_mixed_launcher_and_engine_fail_before_a_live_cycle(self):
        with self.assertRaisesRegex(RuntimeError,'Неповне оновлення'):
            core._verify_runtime(SimpleNamespace(VERSION='old'))
        core._verify_runtime(engine)
        with patch.object(core,'BOT_VERSION','old'):
            with self.assertRaisesRegex(ValueError,'Неповне оновлення'):
                engine.run_live(core)

    def test_legacy_queue_is_labeled_without_erasing_its_events(self):
        state=engine.new_state(engine.Config())
        original='📊 BZ-USDT\nНайближчі сетапи:\n#001 ЗАКРИТО +1R'
        state['notification_queue']=[{'id':'old','text':original}]
        sent=[]
        with tempfile.TemporaryDirectory() as tmp,patch.object(engine,'http_json',side_effect=lambda url,data:(sent.append(data['text']) or {'ok':True})):
            self.assertTrue(engine.notify_queue(state,'test','test',Path(tmp)/'state.json'))
        self.assertIn('Збережений звіт старого формату з черги',sent[0])
        self.assertIn('#001 ЗАКРИТО +1R',sent[0])
        self.assertFalse(state['notification_queue'])


if __name__=='__main__': unittest.main()
