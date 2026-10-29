"""Regression checks for the scheduled 15-minute fast-runtime path."""
import json
import os
from pathlib import Path
import tempfile
import unittest

import full_limit_engine as engine

ROOT = Path(__file__).resolve().parent


class FastRuntimeTests(unittest.TestCase):
    def test_compact_json_roundtrip_preserves_entire_ledger(self):
        sample = {
            'schema': 'test',
            'trades': [{'id': 't1', 'direction': 'SHORT', 'r': -1.09}],
            'orders': [{'id': 'o1', 'status': 'OPEN', 'price': 110.29}],
            'events': [{'id': 'e1', 'text': '✅ ЗАКРИТО • 1.25R'}],
            'unknown_legacy_field': {'preserved': [True, None, 0.0]},
        }
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'state.json'
            engine.atomic_write(path, sample)
            self.assertEqual(engine.read_json(path), sample)
            data = path.read_text(encoding='utf-8')
            self.assertNotIn('  "trades"', data)
            self.assertNotIn('\\u2705', data)
            self.assertFalse((path.with_suffix('.json.tmp')).exists())

    def test_nonfinite_numbers_never_replace_valid_state(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / 'state.json'
            engine.atomic_write(path, {'safe': 1})
            with self.assertRaises(ValueError):
                engine.atomic_write(path, {'unsafe': float('nan')})
            self.assertEqual(engine.read_json(path), {'safe': 1})

    def test_historical_anchor_lookup_preserves_exact_duplicate_rules(self):
        from types import SimpleNamespace
        memory = {
            'one': {'setup_type': 'MSS_REVERSAL_SHORT', 'side': 'SHORT', 'level': 110.29,
                    'evidence': {'setup_timeframe': '1H', 'entry_route': 'MICRO_PRECURSOR',
                                 'zone_identity': 'fixed-zone'}},
            'two': {'setup_type': 'RANGE_EDGE_REVERSAL', 'side': 'LONG', 'level': 109.7,
                    'evidence': {}},
        }
        indexed = engine._historical_anchor_index(memory)
        candidate = SimpleNamespace(setup_type='MSS_REVERSAL_SHORT', side='SHORT',
                                    level=110.293, evidence={'entry_route': 'MICRO_PRECURSOR'})
        self.assertTrue(engine._anchor_already_seen(indexed,candidate,'1H','different-zone',0.01))
        candidate.level = 110.296
        self.assertFalse(engine._anchor_already_seen(indexed,candidate,'1H','different-zone',0.01))
        self.assertTrue(engine._anchor_already_seen(indexed,candidate,'1H','fixed-zone',0.01))
        candidate.level = 110.293
        self.assertFalse(engine._anchor_already_seen(indexed,candidate,'4H','different-zone',0.01))
        candidate.evidence['entry_route']='NATIVE_PATTERN'
        self.assertFalse(engine._anchor_already_seen(indexed,candidate,'1H','different-zone',0.01))

    def test_both_workflows_keep_one_15m_cycle_and_shallow_checkout(self):
        live = (ROOT/'.github/workflows/main.yml').read_text()
        self.assertEqual(live, (ROOT/'WORKFLOW_main.yml').read_text())
        self.assertIn("cron: '*/15 * * * *'", live)
        self.assertIn('fetch-depth: 1', live)
        self.assertNotIn('git fetch --unshallow', live)
        self.assertIn('git rebase --onto', live)
        self.assertIn("BOT_STAGE_TIMING: 'true'", live)
        self.assertNotIn('fetch-depth: 0', live)
        self.assertIn('python bot_oneshot.py --notify', live)
        self.assertNotIn('python bot_oneshot.py --self-test', live)


if __name__ == '__main__':
    unittest.main()
