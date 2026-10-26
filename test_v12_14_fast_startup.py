"""Fast runtime checks without bypassing live data integrity."""
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import bot_oneshot as bot


class ParallelMarketDataTests(unittest.TestCase):
    def test_independent_public_feeds_start_in_parallel(self):
        # Six independent requests must start before the first finishes; avoid
        # wall-clock timing assertions which would be flaky in GitHub Actions.
        gate = threading.Barrier(6, timeout=4)
        called = []
        lock = threading.Lock()

        def record(key, value):
            with lock:
                called.append(key)
            gate.wait()
            return value

        def candles(inst, bar, limit):
            return record(('candles', bar, inst), [])

        def higher(bar, limit, minutes):
            return record(('htf', bar), ([], 'UNAVAILABLE'))

        with patch.object(bot, 'resolve_smt_asset_id', return_value='BTC-USDT-SWAP'), \
             patch.object(bot, 'get_okx_candles', side_effect=candles), \
             patch.object(bot, 'fetch_timeframe', side_effect=higher), \
             patch.object(bot, 'get_okx_ticker', side_effect=lambda inst: record('ticker', {})), \
             patch.object(bot, 'get_tradingview_price_fallback', return_value={}):
            snapshot = bot.collect_market_data()
        self.assertEqual(len(called), 6)
        self.assertEqual(snapshot['execution_price_trusted'], True)
        self.assertEqual(snapshot['price'], 0)
        self.assertEqual(snapshot['smt_asset_id'], 'BTC-USDT-SWAP')

    def test_untrusted_fallback_remains_untrusted_in_parallel_mode(self):
        with patch.object(bot, 'resolve_smt_asset_id', return_value=''), \
             patch.object(bot, 'get_okx_candles', return_value=[]), \
             patch.object(bot, 'fetch_timeframe', return_value=([], 'UNAVAILABLE')), \
             patch.object(bot, 'get_okx_ticker', return_value={}), \
             patch.object(bot, 'get_tradingview_price_fallback', return_value={'price': 109.55}):
            snapshot = bot.collect_market_data()
        self.assertEqual(snapshot['price'], 109.55)
        self.assertFalse(snapshot['execution_price_trusted'])
        self.assertEqual(snapshot['price_source'], 'TRADINGVIEW_FALLBACK')


class WorkflowChecks(unittest.TestCase):
    def test_live_workflow_does_not_run_full_suite_before_market_scan(self):
        root = Path(__file__).resolve().parent
        live = (root/'.github/workflows/main.yml').read_text()
        quality = (root/'.github/workflows/bzu-quality.yml').read_text()
        self.assertIn('Fast preflight', live)
        self.assertIn('Run full v12 signal cycle', live)
        self.assertLess(live.index('Fast preflight'), live.index('Run full v12 signal cycle'))
        self.assertNotIn('python bot_oneshot.py --self-test', live)
        self.assertNotIn('python bot_oneshot.py --legacy-self-test', live)
        self.assertIn('python bot_oneshot.py --self-test', quality)
        self.assertIn('python bot_oneshot.py --legacy-self-test', quality)
        self.assertIn('cancel-in-progress: false', live)
        self.assertIn("MARKET_FETCH_TIMING: 'true'", live)


if __name__ == '__main__':
    unittest.main()
