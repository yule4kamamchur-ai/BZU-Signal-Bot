"""Validate 5m GitHub-only scans and at-most-one 15m Telegram report.

These tests never connect to Telegram or OKX and do not mutate live state.
"""
from pathlib import Path
import unittest

import full_limit_engine as engine

ROOT=Path(__file__).resolve().parent


class GitHubOnlyFifteenMinuteCadence(unittest.TestCase):
    def test_workflow_is_github_only_and_copies_match(self):
        main=(ROOT/'.github/workflows/main.yml').read_text(encoding='utf8')
        self.assertEqual(main,(ROOT/'WORKFLOW_main.yml').read_text(encoding='utf8'))
        self.assertIn("cron: '*/5 * * * *'", main)
        self.assertIn('run: python bot_oneshot.py --auto-report',main)
        self.assertIn("TELEGRAM_NOTIFY_EVERY_RUN: 'false'",main)
        self.assertNotIn('BZU_EXECUTION_MODE',main)
        self.assertNotIn('continuous_worker.py',main)
        self.assertNotIn('docker compose',main)
        self.assertNotIn('python bot_oneshot.py --notify',main)
        self.assertIn('concurrency:',main)
        self.assertIn('cancel-in-progress: false',main)

    def test_reports_only_once_each_15m_bucket(self):
        start=1_800_000_000_000 // 900_000 * 900_000  # UTC aligned
        last=start-900_000
        sends=[]
        for minute in range(0,46,5):
            now=start+minute*60_000
            if engine.report_due(now,last):
                sends.append(minute)
                last=now
        self.assertEqual(sends,[0,15,30,45])

    def test_late_runs_do_not_send_duplicate_report(self):
        start=1_800_000_000_000 // 900_000 * 900_000
        last=start
        self.assertFalse(engine.report_due(start+13*60_000,last))
        self.assertTrue(engine.report_due(start+17*60_000,last))
        last=start+17*60_000
        self.assertFalse(engine.report_due(start+19*60_000,last))
        self.assertTrue(engine.report_due(start+30*60_000,last))


if __name__=='__main__':
    unittest.main()
