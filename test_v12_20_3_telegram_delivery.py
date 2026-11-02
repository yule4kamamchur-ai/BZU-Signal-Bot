"""Telegram delivery is independent of 5m market scans (no real HTTP/API calls)."""
import io
import json
import tempfile
import time
import unittest
import urllib.error
from contextlib import redirect_stderr
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import full_limit_engine as engine


class TelegramCadenceRecoveryTests(unittest.TestCase):
    def exercise(self, *, notify=False, auto_report=False, queued=False, duplicate=False):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            core=SimpleNamespace(BOT_VERSION=engine.VERSION, STATE_FILE=str(root/'state.json'),
                                 JOURNAL_FILE=str(root/'journal.json'),
                                 TELEGRAM_TOKEN='demo-test-token',TELEGRAM_CHAT_ID='123')
            now=int(time.time()*1000)
            state=engine.new_state(engine.Config(core=core))
            state['last_report_requested_ts']=now
            if queued:
                state['notification_queue']=[{'id':'old', 'text':'Звіт із черги', 'ts':now-900_000}]
            engine.atomic_write(Path(core.STATE_FILE),state)
            delivered=[]
            def fake_delivery(updated, token, chat_id, state_path):
                delivered.extend(entry['text'] for entry in updated['notification_queue'])
                updated['notification_queue'].clear()
                engine.atomic_write(state_path,updated)
                return True
            result={'action':'DUPLICATE_OR_OUT_OF_ORDER' if duplicate else 'WATCH',
                    'events':[], 'analysis':{}}
            fake_snap=SimpleNamespace(now=now,metadata={})
            with patch.object(engine,'resolve_live_config',lambda cfg:cfg), \
                 patch.object(engine,'collect_snapshot',lambda cfg,checkpoint: fake_snap), \
                 patch.object(engine.quality,'seed',lambda *a,**kw:None), \
                 patch.object(engine,'run_cycle',lambda *a,**kw:result), \
                 patch.object(engine.research_evidence,'record',lambda *a,**kw:{}), \
                 patch.object(engine,'export_journal',lambda *a,**kw:None), \
                 patch.object(engine,'build_message',lambda *a,**kw:'Звіт нового циклу'), \
                 patch.object(engine,'notify_queue',fake_delivery):
                rc=engine.run_live(core,None,notify=notify,auto_report=auto_report)
            saved=engine.read_json(Path(core.STATE_FILE))
            self.assertEqual(saved['notification_queue'],[])
            return rc, delivered, saved

    def test_manual_run_always_sends_current_report(self):
        rc, delivered, saved=self.exercise(notify=True)
        self.assertEqual(rc,0)
        self.assertEqual(delivered,['Звіт нового циклу'])
        self.assertGreater(saved['last_report_requested_ts'],0)

    def test_silent_5m_scan_retries_old_report_without_new_one(self):
        rc, delivered, _=self.exercise(auto_report=True,queued=True)
        self.assertEqual(rc,0)
        self.assertEqual(delivered,['Звіт із черги'])

    def test_silent_5m_scan_does_not_send_a_new_report(self):
        rc, delivered, _=self.exercise(auto_report=True)
        self.assertEqual(rc,0)
        self.assertFalse(delivered)

    def test_duplicate_market_cycle_still_delivers_manual_report(self):
        rc, delivered, _=self.exercise(notify=True,duplicate=True)
        self.assertEqual(rc,0)
        self.assertEqual(delivered,['Звіт нового циклу'])

    def test_duplicate_market_cycle_retries_previous_failed_report(self):
        rc, delivered, _=self.exercise(auto_report=True,queued=True,duplicate=True)
        self.assertEqual(rc,0)
        self.assertEqual(delivered,['Звіт із черги'])

    def test_telegram_api_error_is_actionable_but_token_is_never_logged(self):
        state={'notification_queue':[{'id':'a','text':'test'}]}
        response=io.BytesIO(json.dumps({'ok':False,'description':'Bad Request: chat not found'}).encode())
        error=urllib.error.HTTPError('https://api.telegram.org/botSECRET/sendMessage',400,
                                      'Bad Request',None,response)
        with tempfile.TemporaryDirectory() as tmp, patch.object(engine,'http_json',side_effect=error), \
             redirect_stderr(io.StringIO()) as log:
            self.assertFalse(engine.notify_queue(state,'SECRET','123',Path(tmp)/'state.json'))
            output=log.getvalue()
        self.assertIn('HTTP 400',output)
        self.assertIn('chat not found',output)
        self.assertNotIn('SECRET',output)
        self.assertEqual(len(state['notification_queue']),1)


if __name__=='__main__':
    unittest.main()
