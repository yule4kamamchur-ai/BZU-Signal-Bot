"""Forensic walk-forward *coverage* audit, not a claimed strategy backtest.

Checks new causal gate against historical paper orders ONLY when archived source
candles were actually available at decision time. Never interprets filtered
historical PnL as out-of-sample profitability. Writes no production state.

Usage:
    python edge_research.py --output edge-audit.json
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import edge_intelligence as edge
from full_limit_engine import Candle, Snapshot

STEPS = {'1H': 3_600_000, '4H': 14_400_000}


def assess_history(trades, archive):
    history = archive.get('candles') or {}
    first_seen = archive.get('first_seen') or {}
    audited = []
    for t in sorted(trades, key=lambda t: (t.get('placed_ts') or 0, t.get('id', ''))):
        ts = int(t.get('placed_ts') or 0)
        if not ts or t.get('status') != 'CLOSED':
            continue
        bars = {}
        coverage = {}
        for tf, step in STEPS.items():
            rows=[]
            for row in history.get(tf, []):
                opened = int(row[0])
                observed = (first_seen.get(tf, {}).get(str(opened)) or {}).get('observed_ts')
                # Both closed time AND actual observation time are required.
                if opened + step <= ts and observed is not None and int(observed) <= ts:
                    rows.append(Candle.parse(row))
            bars[tf] = rows
            coverage[tf] = len(rows)
        sample = {'id': t.get('id'), 'setup': t.get('setup'), 'side': t.get('side'),
                  'entry_version': t.get('bot_version_at_entry'),
                  'placed_ts': ts, 'archived_as_of_coverage': coverage}
        if coverage['1H'] < 13 or coverage['4H'] < 9:
            sample['reason'] = 'INSUFFICIENT_PREDECISION_ARCHIVE'
            sample['eligible_for_counterfactual'] = False
        else:
            snap = Snapshot(ts, float(t['placement_price'] or t['entry']), bars)
            decision = edge.assess(t, snap, t.get('ict_evidence'))
            sample['reason'] = decision['reason']
            sample['eligible_for_counterfactual'] = True
            sample['accepted'] = decision['status'] == 'ALLOW'
        audited.append(sample)
    counts = Counter(a['reason'] for a in audited)
    return {
        'schema': 'BZU_EDGE_V1_FORENSIC_COVERAGE',
        'qualification': ('Post-hoc availability/compatibility audit, NOT a validated '
                          'profitability estimate; rejected orders do not imply profits.'),
        'closed_trades': len(audited),
        'eligible_counterfactual_trades': sum(a.get('eligible_for_counterfactual', False) for a in audited),
        'reasons': dict(counts), 'trades': audited,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--archive', default='research_market_v12_6.json')
    parser.add_argument('--journal', default='signal_journal_v6_4.json')
    parser.add_argument('--output', default='edge-audit.json')
    args = parser.parse_args()
    archive = json.loads(Path(args.archive).read_text())
    journal = json.loads(Path(args.journal).read_text())
    audit = assess_history(journal.get('trades', []), archive)
    Path(args.output).write_text(json.dumps(audit, ensure_ascii=False, indent=2)+'\n')
    print(f"Coverage: {audit['eligible_counterfactual_trades']}/{audit['closed_trades']} "
          'closed trades with causally observed HTF candles')
    print('Reasons:', audit['reasons'])


if __name__ == '__main__':
    main()
