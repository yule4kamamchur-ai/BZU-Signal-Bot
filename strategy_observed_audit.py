"""Point-in-time opportunity counts, not a backtest or a P&L forecast.

Uses recorded first_seen timestamps; never labels from bars not yet observed.
Run independently, outside scheduled 15m production job:
    python strategy_observed_audit.py --archive research_market_v12_6.json
"""
from __future__ import annotations

import argparse
import bisect
import collections
import json
from pathlib import Path
from types import SimpleNamespace

import entry_playbooks
import strategy_organics


def observed_counts(path, limit=180):
    source = json.loads(Path(path).read_text())
    if source.get('schema') != 'bzu_controlled_market_v2':
        raise ValueError('Only the bounded first_seen runtime archive is supported')
    quotes = sorted((q for q in source.get('quotes', [])
                     if q.get('trusted') is True and q.get('price', 0) > 0),
                    key=lambda q: q['now'])
    # One recorded snapshot per 15m interval (not made-up exact cron executions).
    by_interval = {}
    for q in quotes:
        by_interval.setdefault(int(q['now']) // 900_000, q)
    selected = list(by_interval.values())[-limit:]
    observed = {}
    for tf, duration in strategy_organics.STEP.items():
        first = source.get('first_seen', {}).get(tf, {})
        rows = []
        for raw in source.get('candles', {}).get(tf, []):
            timestamp = int(raw[0])
            meta = first.get(str(timestamp), {})
            seen = int(meta.get('observed_ts', 2**63))
            if str(raw[8]) == '1' and seen < 2**63:
                rows.append((timestamp, seen, SimpleNamespace(ts=timestamp,open=float(raw[1]),
                    high=float(raw[2]),low=float(raw[3]),close=float(raw[4]),confirmed=True)))
        observed[tf] = rows
    report = collections.Counter()
    by_strategy = collections.Counter()
    by_side = collections.Counter()
    for q in selected:
        now = int(q['now'])
        candles = {}
        for tf, duration in strategy_organics.STEP.items():
            candles[tf] = [bar for start, seen, bar in observed[tf]
                           if seen <= now and start+duration <= now]
        s = SimpleNamespace(now=now, price=float(q['price']), candles=candles)
        report['observed_scans'] += 1
        if len(candles['3m']) < 40 or len(candles['15m']) < 25:
            report['insufficient_warmup'] += 1
            continue
        possibilities = entry_playbooks.scenarios(s, float(q.get('tick') or .01))
        report['raw_playbook_events'] += len(possibilities)
        regime = strategy_organics.classify(s)
        report['regime_'+regime.state] += 1
        for c in possibilities:
            outcome = strategy_organics.qualify(c,s,float(q.get('tick') or .01),regime)
            if not outcome['accepted']:
                report['regime_refused'] += 1
                continue
            # This is NOT a trade: no native candidate/targets/orderbook/fill.
            by_strategy[outcome['strategy']] += 1
            by_side['LONG' if c.side == 1 else 'SHORT'] += 1
    return {'source': str(path),'kind': 'OBSERVED_OPPORTUNITY_COUNTS_ONLY',
            'not_backtest': True, 'not_independent_trade_count': True,
            'counts': dict(report),'candidate_events_by_strategy':dict(by_strategy),
            'candidate_events_by_side':dict(by_side),
            'limitation': 'No native planner, no fills, no realized R. Recorded scans may be irregular.'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--archive', default='research_market_v12_6.json')
    parser.add_argument('--output',default='organic-opportunities.json')
    args=parser.parse_args()
    output=observed_counts(args.archive)
    Path(args.output).write_text(json.dumps(output,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(output,ensure_ascii=False,indent=2))
