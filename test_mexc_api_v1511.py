"""Regression tests for the official REST migration and API denial handling."""
import json
import tempfile
import time
import unittest
from collections import Counter
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit, parse_qs

import mexc_multi_scanner as ms
from test_mexc_multi_scanner import _make_klines

SYMBOLS = [s['symbol'] for s in ms.INSTRUMENTS] + ['BTC_USDT']


def mock_good(url):
    path = urlsplit(url).path
    if path.endswith('/detail'):
        return {'success': True, 'data': [{'symbol': s, 'priceUnit': .01} for s in SYMBOLS]}
    if path.endswith('/ticker'):
        return {'success': True, 'data': [{'symbol': s, 'lastPrice': 100, 'bid1': 99.99, 'ask1': 100.01} for s in SYMBOLS]}
    q = parse_qs(urlsplit(url).query)
    step = ms.TF_SECONDS[q['interval'][0]]
    end = int(q['end'][0]) // step * step
    return _make_klines(end - 145 * step, step, 145, 100, .001)


def forbidden(url):
    return HTTPError(url, 403, 'Forbidden', None, None)


class APIHealthTests(unittest.TestCase):
    def test_default_rest_domain(self):
        self.assertEqual(ms.MEXC_BASE, 'https://api.mexc.com')
        seen=[]
        def fetch(url):
            seen.append(url)
            return mock_good(url)
        ms.run_scan(fetcher=fetch)
        self.assertTrue(all(url.startswith('https://api.mexc.com/api/v1/contract/') for url in seen))

    def test_global_denial_prevents_fanout_and_keeps_last_success(self):
        calls=[]
        def blocked(url):
            calls.append(url)
            raise forbidden(url)
        with tempfile.TemporaryDirectory() as d:
            state, mem = Path(d)/'state.json', Path(d)/'memory.json'
            first=ms.run_cycle(state, mem, fetcher=mock_good)
            self.assertEqual(first['health']['fresh_quotes'], 7)
            prior=first['updated_ts']
            # Simulate a later failed scan.
            time.sleep(.002)
            failed=ms.run_cycle(state,mem,fetcher=blocked)
            self.assertEqual(len(calls),2)
            self.assertEqual(failed['updated_ts'],prior)
            self.assertGreater(failed['last_attempt_ts'],prior)
            self.assertEqual(failed['health']['status'],'FAILED_DATA')
            self.assertEqual(failed['health']['fresh_quotes'],0)
            self.assertEqual(failed['health']['fresh_analysis_count'],0)
            self.assertTrue(failed['health']['fanout_skipped'])
            self.assertEqual(failed['health']['api_http_statuses'],{'DETAIL/':403,'TICKER':403})
            self.assertEqual(failed['ready_count'],0)
            self.assertEqual(json.loads(state.read_text()),failed)
            recovered=ms.run_cycle(state,mem,fetcher=mock_good)
            self.assertEqual(recovered['health']['status'],'OK')
            self.assertEqual(recovered['health']['fresh_quotes'],7)
            self.assertEqual(recovered['health']['fresh_analysis_count'],7)
            self.assertFalse(recovered['health']['access_denied'])
            self.assertGreater(recovered['updated_ts'],prior)

    def test_single_denied_endpoint_does_not_retry_or_disable_other_assets(self):
        calls=Counter()
        def fetch(url):
            path=urlsplit(url).path
            tf=parse_qs(urlsplit(url).query).get('interval',[''])[0]
            calls[path,tf]+=1
            if path.endswith('/APT_USDT') and tf=='Min1': raise forbidden(url)
            return mock_good(url)
        result=ms.run_scan(fetcher=fetch)
        self.assertEqual(calls['/api/v1/contract/kline/APT_USDT','Min1'],1)
        self.assertFalse(result['health']['fanout_skipped'])
        self.assertEqual(result['health']['status'],'DEGRADED')
        self.assertTrue(next(r for r in result['instruments'] if r['symbol']=='LINK_USDT')['analysis_fresh'])
        self.assertFalse(next(r for r in result['instruments'] if r['symbol']=='APT_USDT').get('analysis_fresh'))

    def test_detail_denied_alone_does_not_prevent_candle_collection(self):
        calls=Counter()
        def fetch(url):
            path=urlsplit(url).path
            calls[path]+=1
            if path.endswith('/detail'): raise forbidden(url)
            return mock_good(url)
        result=ms.run_scan(fetcher=fetch)
        self.assertEqual(calls['/api/v1/contract/detail'],1)
        self.assertEqual(calls['/api/v1/contract/ticker'],2)
        self.assertFalse(result['health']['fanout_skipped'])
        self.assertEqual(len(result['instruments']),7)
        self.assertEqual(result['ready_count'],0)

    def test_workflow_publishes_before_marking_failure(self):
        y=Path('.github/workflows/main.yml').read_text()
        self.assertIn('workflow_dispatch:',y)
        self.assertIn("MEXC_FUTURES_BASE: 'https://api.mexc.com'",y)
        self.assertIn('data_status: ${{ steps.scan.outputs.data_status }}',y)
        self.assertLess(y.index('Persist small MEXC state only'),y.index('Publish dashboard'))
        self.assertLess(y.index('Publish dashboard'),y.index('Verify market data after publication'))
        self.assertIn('DATA_STATUS: ${{ needs.scan.outputs.data_status }}',y)

if __name__=='__main__': unittest.main()
