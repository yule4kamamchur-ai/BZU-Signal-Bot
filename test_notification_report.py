"""User-facing state separation, complete close delivery, and cohort integrity."""
import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import full_limit_engine as engine
import notification_report as report
from analyze_bot import audit, archive_integrity
from test_full_limit_engine import NOW, order, snapshot


class CompactReports(unittest.TestCase):
    def setUp(self):
        self.cfg = engine.Config()
        self.state = engine.new_state(self.cfg)
        self.snap = snapshot()

    def text(self, result=None):
        engine.bind_order_numbers(self.state)
        return engine.build_message(self.state, self.snap, result or {'action': 'WATCH'}, self.cfg)

    def test_portfolio_rotation_message_only_reports_actual_commit(self):
        self.state['events']=[{'kind':'PORTFOLIO_ROTATION_COMMITTED','ts':NOW,
                               'replaced_id':'old','replacement_id':'new'}]
        result={'action':'PLACE_LIMIT','events':self.state['events']}
        text=self.text(result)
        self.assertIn('Заміна ліміту:',text)
        self.assertIn('без третього ордера',text)

    def test_staged_rotation_does_not_pretend_to_be_an_open_limit(self):
        self.state['portfolio_rotation_intent']={
            'status':'PREPARED_WAIT_RECONCILIATION','candidate_id':'future',
            'replaced_id':'old'}
        text=self.text({'action':'CANCEL_LIMIT'})
        self.assertIn('перевіряємо виконання старого ліміту',text)
        self.assertIn('Ліміти в очікуванні: 0',text)

    def test_pending_and_active_have_distinct_sections_and_stable_numbers(self):
        active = order()
        active.update(id='opened', order_number=5, status='OPEN', remaining=.35,
                      exit_deadline=NOW+3600000, tp1_hit=True, partials={'TP1': .65, 'TP2': .35}, tp2=104.)
        pending = order()
        pending.update(id='waiting', order_number=8, entry=99., status='PENDING')
        self.state['active'] = active
        self.state['second_slot']['pending'] = pending
        before = copy.deepcopy(self.state)
        text = self.text()
        followed, waiting = text.split('ОЧІКУЄМО ВХОДУ — 1')
        self.assertIn('У СУПРОВОДІ — 1', followed)
        self.assertIn('#005 LONG', followed)
        self.assertNotIn('#008 LONG', followed)
        self.assertIn('#008 LONG', waiting)
        self.assertNotIn('Залишок', followed)
        self.assertIn('TP2: 104', followed)
        for key in ('active', 'second_slot'):
            self.assertEqual(self.state[key]['entry'] if key == 'active' else self.state[key]['pending']['entry'],
                             before[key]['entry'] if key == 'active' else before[key]['pending']['entry'])

    def test_two_pending_orders_do_not_create_a_third_scenario_or_fake_open_position(self):
        self.state['pending'] = order()
        second = order(); second.update(id='second', entry=99.)
        self.state['second_slot']['pending'] = second
        result = {'action': 'WAIT_LIMIT', 'analysis': {'watch': [
            {'setup': 'SWEEP_RECLAIM', 'timeframe': '15m', 'side': 1,
             'level': 98., 'readiness_pct': 99., 'reason': 'ACCEPTED'}]}}
        text = self.text(result)
        self.assertNotIn('У СУПРОВОДІ — 0', text)
        self.assertIn('ОЧІКУЄМО ВХОДУ — 2', text)
        self.assertNotIn('НА СПОСТЕРЕЖЕННІ', text)
        self.assertNotIn('готовність', text)
        self.assertLess(len(text), 1500)

    def test_closures_do_not_include_expired_orders_in_win_rate(self):
        trade = order(); trade.update(id='won', version=engine.VERSION, closed_ts=NOW,
                                      close_reason='STOP', net_r=.3, tp1_hit=True)
        self.state['trades'] = [trade]
        result = {'action': 'WATCH', 'events': [
            {'kind': 'CLOSED', 'order_id': 'won', 'ts': NOW, 'net_r': .3, 'reason': 'STOP'},
            {'kind': 'EXPIRED', 'order_id': 'unused', 'ts': NOW, 'reason': 'LIMIT_TTL'}]}
        text = self.text(result)
        self.assertIn('ЗАКРИТО: +0.30R — прибуток', text)
        self.assertIn('TP1 досягнуто; стоп', text)
        self.assertIn('строк минув, входу не було', text)
        self.assertIn('Сьогодні (Київ): 1 закрита угода • результат +0.30R', text)

    def test_an_old_close_is_labeled_as_previous_and_not_new(self):
        trade = order(); trade.update(closed_ts=NOW-3600000, net_r=-1., close_reason='STOP')
        self.state['trades'] = [trade]
        text = self.text()
        self.assertIn('Нових закриттів немає', text)
        self.assertIn('Остання закрита:', text)
        self.assertNotIn('ЗАКРИТО: -1.00R', text)

    def test_closure_after_upgrade_is_in_daily_summary_not_version_statistics(self):
        old_trade = order()
        old_trade.update(id='closed-previous', order_id='closed-previous',
                         version='full-ict-v12.10.0-causal-direction-control',
                         entry_version='full-ict-v12.10.0-causal-direction-control',
                         exit_version=engine.VERSION, closed_ts=NOW-1000,
                         net_r=1.69, close_reason='ADAPTIVE_FULL_EXIT')
        self.state['trades'] = [old_trade]
        text = self.text()
        self.assertIn('Сьогодні (Київ): 1 закрита угода • результат +1.69R', text)
        self.assertNotIn('Усі закриті угоди', text)
        self.assertNotIn('Угоди, відкриті за', text)
        # The core trade cohort remains unchanged.
        self.assertEqual(engine.trade_entry_version(old_trade),
                         'full-ict-v12.10.0-causal-direction-control')

    def test_daily_summary_excludes_other_days_and_future_closes(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        kyiv = ZoneInfo(self.cfg.timezone)
        now = datetime.fromtimestamp(NOW/1000, kyiv)
        previous_day = datetime.fromtimestamp((NOW-24*3600000)/1000, kyiv)
        trade_old = order()
        trade_old.update(id='old', closed_ts=int(previous_day.timestamp()*1000), net_r=-1.0)
        trade_new = order()
        trade_new.update(id='today', closed_ts=NOW-1000, net_r=.8)
        future = order()
        future.update(id='future', closed_ts=NOW+1000, net_r=99.)
        unclosed = order()
        unclosed.update(id='unclosed', closed_ts=None, net_r=99.)
        self.state['trades'] = [trade_old,trade_new,future,unclosed]
        text = self.text()
        self.assertIn('Сьогодні (Київ): 1 закрита угода • результат +0.80R', text)
        self.assertNotIn('WR ', text)

    def test_new_and_legacy_closures_only_show_same_day_total(self):
        old = order()
        old.update(id='older', order_id='older', version='full-ict-v12.10.0-older',
                   net_r=-1., closed_ts=NOW-2000)
        current = order()
        current.update(id='newer', order_id='newer', entry_version=engine.VERSION,
                       version=engine.VERSION, net_r=1.7, closed_ts=NOW-1000)
        self.state['trades'] = [old, current]
        text = self.text()
        self.assertIn('Сьогодні (Київ): 2 закриті угоди • результат +0.70R', text)
        self.assertNotIn('Усі закриті угоди', text)
        self.assertNotIn('Угоди, відкриті за', text)
        self.assertEqual(engine.statistics_by_entry_version(self.state['trades'])[engine.VERSION]['trades'], 1)

    def test_queued_history_is_removed_without_erasing_close_event(self):
        old_text = ('ЗАКРИТО З МИНУЛОГО ЗВІТУ\n#025 ЗАКРИТО: -1.09R\n'
                    'Усі закриті угоди (історія всіх версій): 25 • WR 24.0% (6/25) • -9.45R\n'
                    'Угоди, відкриті за v12.13.0: WR 0.0% (0/1) • -1.09R\n'
                    'Останнє закриття зараховано до версії входу v12.10.0\n')
        clean = report.clean_queued_message(old_text)
        self.assertIn('#025 ЗАКРИТО: -1.09R', clean)
        self.assertNotIn('Усі закриті угоди', clean)
        self.assertNotIn('Угоди, відкриті за', clean)

    def test_obsolete_statistics_only_queue_page_does_not_block_next_close(self):
        from unittest.mock import patch
        self.state['notification_queue'] = [
            {'text':'Усі закриті угоди (історія всіх версій): 25 • WR 24.0% (6/25) • -9.45R',
             'version':engine.VERSION},
            {'text':'#025 ЗАКРИТО: -1.09R', 'version':engine.VERSION},
        ]
        with patch.object(engine, 'http_json', return_value={'ok':True}) as send, \
             patch.object(engine, 'atomic_write'):
            success=engine.notify_queue(self.state,'test-token','test-chat',Path('unused.json'))
        self.assertTrue(success)
        self.assertEqual(send.call_count, 1)
        self.assertIn('#025 ЗАКРИТО: -1.09R', send.call_args.args[1]['text'])
        self.assertFalse(self.state['notification_queue'])

    def test_midnight_kyiv_day_not_utc_day(self):
        from datetime import datetime
        from zoneinfo import ZoneInfo
        now = datetime(2026,10,26,0,10,tzinfo=ZoneInfo('Europe/Kyiv'))
        prior = datetime(2026,10,25,23,55,tzinfo=ZoneInfo('Europe/Kyiv'))
        today_trade = order();today_trade.update(id='today', closed_ts=int(now.timestamp()*1000),net_r=.5)
        old = order();old.update(id='yesterday', closed_ts=int(prior.timestamp()*1000),net_r=-2.)
        self.snap.now = int(now.timestamp()*1000)
        self.snap.ticker_ts = self.snap.now
        self.state['trades'] = [today_trade,old]
        text=self.text()
        self.assertIn('Сьогодні (Київ): 1 закрита угода • результат +0.50R',text)

    def test_unknown_result_keeps_reserved_slot_visible(self):
        self.state['reconciliation'] = {'order_id': 'uncertain', 'ts': NOW}
        text = self.text({'action': 'RECONCILIATION_REQUIRED'})
        self.assertIn('Відкриті угоди: 0 • Ліміти в очікуванні: 0', text)
        self.assertIn('виконання НЕВИЗНАЧЕНЕ; потрібна перевірка', text)

    def test_stale_lifecycle_never_presents_saved_position_as_fresh_result(self):
        active = order(); active.update(status='OPEN', exit_deadline=NOW+3600000)
        self.state['active'] = active
        self.state['last_run_ts'] = NOW-3600000
        text = self.text({'action': 'DATA_HOLD', 'analysis': {'health': ['LIFECYCLE_HISTORY_GAP']}})
        self.assertIn('Дані не оновлені. Збережений стан', text)
        self.assertIn('Результат зараз ≈ недоступний', text)
        self.assertNotIn('LIFECYCLE_HISTORY_GAP', text)

    def test_touch_or_cross_is_not_automatically_reported_as_a_fill(self):
        self.state['pending'] = order(); self.snap.price = 99.
        text = self.text({'action': 'WAIT_LIMIT'})
        self.assertNotIn('У СУПРОВОДІ — 0', text)
        self.assertIn('#001 LONG • Вхід: 100', text)
        self.assertNotIn('виконання перевіряємо',text)

    def test_forecast_percentage_and_stale_readiness_are_not_in_order_card(self):
        p = order()
        p.update(price_forecast={'status': 'MODEL_ONLY', 'tp1_before_sl': .99}, readiness_pct=100.)
        self.state['pending'] = p
        text = self.text()
        for noise in ('99%', '100% готов', 'цінова модель', 'нижня межа 95%', 'ATR', 'PAPER'):
            self.assertNotIn(noise, text)

    def test_formatter_does_not_change_the_ledger(self):
        self.state['pending'] = order(); engine.bind_order_numbers(self.state)
        before = copy.deepcopy(self.state)
        self.text()
        self.assertEqual(self.state, before)

    def test_long_reports_retain_every_closed_order_and_fit_telegram(self):
        events = []
        for n in range(1, 121):
            trade = order(); trade.update(id=str(n), order_number=n, net_r=-1., closed_ts=NOW,
                                          close_reason='STOP', version=engine.VERSION)
            self.state['trades'].append(trade)
            events.append({'kind': 'CLOSED', 'order_id': str(n), 'ts': NOW, 'net_r': -1., 'reason': 'STOP'})
        text = self.text({'action': 'WATCH', 'events': events})
        pages = report.split_message(text)
        self.assertGreater(len(pages), 1)
        self.assertTrue(all(len(page) <= 3800 for page in pages))
        delivered = '\n'.join(pages)
        for n in range(1, 121):
            self.assertEqual(delivered.count(f'#{n:03d} ЗАКРИТО:'), 1)

    def test_retry_keeps_unsent_pages_after_partial_delivery(self):
        self.state['notification_queue'] = [{'text': part, 'version': engine.VERSION}
                                             for part in ('first', 'second', 'third')]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'state.json'
            with patch.object(engine, 'http_json', side_effect=[{'ok': True}, {'ok': False}]):
                self.assertFalse(engine.notify_queue(self.state, 'test', 'test', path))
            self.assertEqual([p['text'] for p in self.state['notification_queue']], ['second', 'third'])
            with patch.object(engine, 'http_json', return_value={'ok': True}):
                self.assertTrue(engine.notify_queue(self.state, 'test', 'test', path))
            self.assertFalse(self.state['notification_queue'])


class CohortAudit(unittest.TestCase):
    def test_exact_test_quotes_are_identified_without_rewriting_the_archive(self):
        market = {'quotes': [{'now': 1791288000000, 'price': 70., 'bid': 69.995, 'ask': 70.005}]}
        before = copy.deepcopy(market)
        result = archive_integrity(market)
        self.assertEqual(result['known_test_quotes'], 1)
        self.assertEqual(market, before)
        market['quotes'][0]['now'] += 1
        self.assertEqual(archive_integrity(market)['known_test_quotes'], 0)

    def test_research_refuses_known_test_data_before_training_or_replay(self):
        import market_archive
        import research_validate
        market = {'schema': market_archive.SCHEMA, 'quotes': [
            {'now': 1791288000000, 'price': 70., 'bid': 69.995, 'ask': 70.005}]}
        with self.assertRaisesRegex(ValueError, 'synthetic self-test'):
            research_validate.archive_dataset(market)

    def test_self_test_isolates_and_restores_the_production_archive_path(self):
        import unittest.mock
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            production = Path(directory)/'production.json'
            production.write_text('real evidence')
            def fake_runner(suite):
                path = Path(os.environ['MARKET_HISTORY_FILE'])
                self.assertNotEqual(path, production)
                path.write_text('synthetic evidence')
                return SimpleNamespace(wasSuccessful=lambda: True)
            with patch.dict(os.environ, {'MARKET_HISTORY_FILE': str(production)}), \
                 patch('sys.argv', ['bot_oneshot.py', '--self-test']), \
                 patch.object(engine.Config, 'from_env', return_value=engine.Config()), \
                 patch.object(unittest.defaultTestLoader, 'discover', return_value=SimpleNamespace(countTestCases=lambda: 1)), \
                 patch.object(unittest.TextTestRunner, 'run', side_effect=fake_runner):
                self.assertEqual(engine.main(None), 0)
                self.assertEqual(os.environ['MARKET_HISTORY_FILE'], str(production))
            self.assertEqual(production.read_text(), 'real evidence')

    def test_entry_version_wins_over_version_at_close(self):
        rows = [{'id': 'old', 'entry_version': 'previous', 'version': engine.VERSION, 'net_r': 1.},
                {'id': 'new', 'bot_version_at_entry': engine.VERSION, 'net_r': -1.}]
        result = engine.statistics_by_entry_version(rows)
        self.assertEqual(result['previous']['wins'], 1)
        self.assertEqual(result[engine.VERSION]['trades'], 1)
        self.assertEqual(result[engine.VERSION]['win_rate'], 0.)

    def test_closing_a_legacy_order_does_not_relabel_its_entry(self):
        state = engine.new_state(engine.Config()); plan = order()
        plan.update(entry_version='older', remaining=1.)
        state['active'] = plan
        engine.close_trade(state, plan, 101., NOW, 'INTRADAY_TIME_EXIT', engine.Config())
        trade = state['trades'][0]
        self.assertEqual(trade['version'], 'older')
        self.assertEqual(trade['exit_version'], engine.VERSION)

    def test_legacy_version_without_entry_field_is_kept_in_both_cohort_fields(self):
        state = engine.new_state(engine.Config()); plan = order()
        plan.update(version='older', remaining=1.)
        state['active'] = plan
        engine.close_trade(state, plan, 101., NOW, 'INTRADAY_TIME_EXIT', engine.Config())
        trade = state['trades'][0]
        self.assertEqual(trade['bot_version_at_entry'], 'older')
        self.assertEqual(engine.trade_entry_version(trade), 'older')

    def test_journal_exports_current_cohort_separately_from_all_history(self):
        state = engine.new_state(engine.Config())
        state['trades'] = [{'id': 'old', 'net_r': 1., 'version': 'old'},
                           {'id': 'new', 'net_r': -1., 'entry_version': engine.VERSION}]
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp)/'journal.json'; engine.export_journal(path, state)
            saved = engine.read_json(path)['full_v12']
        self.assertEqual(saved['statistics']['trades'], 2)
        self.assertEqual(saved['current_strategy_statistics']['trades'], 1)
        self.assertEqual(saved['statistics_scope'], 'ALL_ENTRY_VERSIONS')

    def test_setup_activity_without_a_hit_is_not_called_dead(self):
        state = engine.new_state(engine.Config())
        state['last_run_ts'] = NOW
        result = audit(state)
        self.assertEqual(len(result['setup_activity']), 24)
        self.assertTrue(all(row['diagnosis'] == 'NOT_OBSERVED_NOT_PROVEN_DEAD'
                            for row in result['setup_activity'].values()))


if __name__ == '__main__':
    unittest.main()
