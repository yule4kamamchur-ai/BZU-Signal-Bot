"""Read-only audit of actual signal outcomes and setup activity; no network."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from dataclasses import replace
import hashlib
import json
from pathlib import Path
from statistics import median

import full_limit_engine as engine
import signal_quality as quality


def archive_integrity(market):
    import market_archive
    return market_archive.known_test_contamination(market)


def audit(state, journal=None, market=None):
    trades = state.get('trades', [])
    versions = engine.statistics_by_entry_version(trades)
    current = [t for t in trades if engine.trade_entry_version(t) == engine.VERSION]
    signals = [s for s in state.get('signals', []) if s.get('version') == engine.VERSION]
    detector_rows = defaultdict(Counter)
    detector_tf = defaultdict(lambda: defaultdict(Counter))
    for signal in signals:
        for detector in signal.get('detectors', []):
            for evaluation in detector.get('timeframes', []):
                detector_rows[detector['setup']][evaluation['status']] += 1
                detector_tf[detector['setup']][evaluation['timeframe']][evaluation['status']] += 1
    import setup_scanner
    per_setup = {}
    for setup in setup_scanner.SETUP_NAMES:
        placed = [o for o in state.get('orders', []) if engine.trade_entry_version(o) == engine.VERSION
                  and o.get('setup') == setup]
        live = [p for p in engine.open_plans(state) if p.get('setup') == setup]
        subset = [t for t in current if t.get('setup') == setup]
        counts = dict(detector_rows[setup])
        per_setup[setup] = {'evaluations': sum(counts.values()), 'detected': counts.get('DETECTED', 0),
                            'errors': counts.get('ERROR', 0), 'by_timeframe': dict(detector_tf[setup]),
                            'orders_finished': len(placed), 'open_plans': len(live),
                            'closed_trade_statistics': engine.statistics(subset),
                            'diagnosis': 'DETECTED_IN_RETAINED_SCANS' if counts.get('DETECTED')
                            else 'NOT_OBSERVED_NOT_PROVEN_DEAD'}
    deltas = [(b['ts']-a['ts'])/60_000 for a, b in zip(signals, signals[1:])]
    quoted = [q for q in (market or {}).get('quotes', []) if q.get('version') == engine.VERSION]
    qdeltas = [(b['now']-a['now'])/60_000 for a, b in zip(quoted, quoted[1:])]
    q = state.get('quality', {})
    observations = q.get('observations', [])
    independent = quality.independent(observations)
    cells = defaultdict(list)
    for row in observations:
        if not row.get('qualified'):
            continue
        f = row['features']
        cells[(f['family'], f['alignment'], f['regime'], f['side'], f.get('setup_timeframe', '15m'))].append(row)
    cell_counts = [len(quality.independent(rows)) for rows in cells.values()]
    closed_ids = {t['id'] for t in trades}
    orders = [o for o in state.get('orders', []) if engine.trade_entry_version(o) == engine.VERSION]
    open_ids = {p['id'] for p in engine.open_plans(state)}
    journal_trades = (journal or {}).get('full_v12', {}).get('trades', [])
    comparison = ({t['id']: t.get('net_r') for t in trades}
                  == {t['id']: t.get('net_r') for t in journal_trades}) if journal else None
    return {
        'source': 'UPLOADED_LOCAL_LEDGERS_ONLY', 'strategy_version': engine.VERSION,
        'as_of_ts': state.get('last_run_ts'), 'as_of_utc': engine.iso(state['last_run_ts']),
        'execution': 'PRICE_SIGNAL_OHLC_SIMULATION',
        'current_strategy': versions.get(engine.VERSION, engine.statistics([])),
        'all_versions': engine.statistics(trades), 'by_entry_version': versions,
        'include_commission': state.get('include_commission'),
        'orders_current_strategy': {'total': len(orders)+sum(p['id'] not in {o['id'] for o in orders}
                                                               for p in engine.open_plans(state)),
                    'finished_status_counts': dict(Counter(o['status'] for o in orders)),
                    'pending': len(engine.pending_plans(state)),
                    'active': sum(bool(v.get('active')) for _, v in engine.slot_views(state))},
        'open_plans': [{k: p.get(k) for k in ('id', 'display_id', 'order_number', 'status', 'side',
                                              'setup', 'setup_timeframe', 'entry', 'stop',
                                              'tp1', 'tp2', 'tp3', 'full_exit_target', 'tp1_hit', 'awaiting_tp1_review', 'placed_ts', 'expires_ts')}
                      for p in engine.open_plans(state)],
        'trade_rows': [{k: t.get(k) for k in ('id', 'order_number', 'side', 'setup', 'timeframe',
                     'placed_ts', 'filled_ts', 'closed_ts', 'entry', 'exit', 'net_r',
                     'close_reason', 'realized_legs')} for t in current],
        'setup_activity': per_setup,
        'scan_window': {'scans': len(signals), 'first_ts': signals[0]['ts'] if signals else None,
                       'last_ts': signals[-1]['ts'] if signals else None,
                       'full_scans': sum(s.get('timeframe_evaluations') == 72 for s in signals),
                       'detector_errors': sum(x['errors'] for x in per_setup.values()),
                       'health': dict(Counter(c for s in signals for c in s.get('health', []))),
                       'rejections': dict(sum((Counter(s.get('rejections', {})) for s in signals), Counter())),
                       'median_interval_minutes': median(deltas) if deltas else None,
                       'max_interval_minutes': max(deltas) if deltas else None,
                       'intervals_over_20m': sum(x > 20 for x in deltas),
                       'intervals_at_most_7m': sum(x <= 7 for x in deltas),
                       'archived_quote_median_interval_minutes': median(qdeltas) if qdeltas else None},
        'counterfactual_evidence': {'closed': len(observations),
                    'all_statistics': engine.statistics(observations),
                    'nonoverlapping_statistics': engine.statistics(independent),
                    'pending': len(q.get('pending', [])),
                    'largest_independent_family_cell': max(cell_counts, default=0),
                    'win_rate_entry_gate_enabled':False,
                    'feedback_mode':'CONTINUOUS_SHRINKAGE_FROM_FIRST_RESOLVED_EPISODE',
                    'unobserved_due_capacity': q.get('unobserved_due_capacity', 0),
                    'is_actual_portfolio_win_rate': False},
        'integrity': {'journal_matches_state_trade_results': comparison,
                      'duplicate_trade_ids': len(trades)-len(closed_ids),
                      'duplicate_open_ids': len(engine.open_plans(state))-len(open_ids),
                      'closed_still_open': sorted(closed_ids & open_ids),
                      'occupied_slots': engine.occupied_slots(state),
                      'unresolved_slots': sum(bool(v.get('reconciliation')) for _, v in engine.slot_views(state))},
        'market_archive_integrity': archive_integrity(market),
        'limits': ['Small samples cannot validate a 70% win rate',
                   'Detector hits are repeated evaluations, not independent opportunities',
                   'A detected setup without a filled trade is not proof of dead code',
                   'Counterfactual scenarios are correlated and are not executed trades',
                   'No exchange account, fills, funding or realized deposit PnL supplied'],
    }


def diagnose_planner(market):
    """Inspect fresh detector anchors; not a portfolio or profitability replay.

    Stored anchors can become valid on a later scan, so zero fresh approvals
    does NOT imply a dead setup. Public archives do not contain the SMT peer.
    """
    import bot_oneshot as core
    import research_validate as research
    import setup_scanner as scanner
    integrity = archive_integrity(market)
    after = integrity['safe_research_after_ts']
    if after is not None:
        # Discard the whole potentially tainted prefix IN MEMORY. Some real
        # older rows may have been displaced by first-observed fixture rows.
        # Preserve the user's original archive unchanged for recovery/audit.
        market = {**market,
                  'quotes': [q for q in market['quotes'] if q['now'] > after],
                  'candles': {tf: [row for row in rows if engine.Candle.parse(row).ts > after]
                              for tf, rows in market['candles'].items()}}
    data = research.archive_dataset(market)
    quotes = {q['now']: q for q in market['quotes']}
    counts = {setup: Counter() for setup in scanner.SETUP_NAMES}
    scans = 0
    for snap in research.snapshots(data):
        quote = quotes[snap.now]
        if quote.get('version') != engine.VERSION:
            continue
        cfg = replace(engine.Config(), core=core, tick=quote['tick'])
        if engine.health(snap, cfg):
            continue
        scans += 1
        state = engine.new_state(cfg)
        with engine.market_clock(core, snap.now):
            context = engine.build_full_context(snap, state, cfg)
            contexts = scanner.build_contexts(snap, context, state, cfg)
            for setup, detector in zip(scanner.SETUP_NAMES, core.DETECTORS):
                for tf, native in contexts.items():
                    anchor = detector(native)
                    if anchor is None:
                        continue
                    anchor.evidence.update(setup_timeframe=tf, micro_timeframe=scanner.MICRO[tf])
                    anchor.expires_ts = anchor.created_ts + (anchor.expires_ts-anchor.created_ts)*scanner.MINUTES[tf]//15
                    _, reason = engine.full_plan(anchor, native, snap, cfg, {})
                    counts[setup][reason] += 1
    return {'scope': 'FRESH_ANCHORS_ONLY_NOT_A_PORTFOLIO_BACKTEST', 'scans': scans,
            'excluded_potentially_tainted_prefix_through_ts': after,
            'configuration': 'Config defaults, observed exchange tick; same 1% SL / 1.5% TP1 floors',
            'limitations': ['No saved anchor memory, portfolio capacity or statistical filter',
                            'No archived SMT peer candles or full original provider metadata',
                            'A zero fresh approval count does not prove a dead setup'],
            'counts': {setup: dict(rows) for setup, rows in counts.items()}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--state', type=Path, default=Path('last_signal_v6_4.json'))
    parser.add_argument('--journal', type=Path, default=Path('signal_journal_v6_4.json'))
    parser.add_argument('--market', type=Path, default=Path('market_history_v12.json'))
    parser.add_argument('--output', type=Path)
    parser.add_argument('--diagnose-planner', action='store_true',
                        help='Also inspect fresh anchors on archived quotes; not a backtest')
    args = parser.parse_args()
    report = audit(engine.read_json(args.state), engine.read_json(args.journal), engine.read_json(args.market))
    if args.diagnose_planner:
        report['planner_diagnostic'] = diagnose_planner(engine.read_json(args.market))
    report['source_sha256'] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                               for p in (args.state, args.journal, args.market) if p.exists()}
    if args.output:
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    else:
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
