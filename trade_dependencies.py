"""Group shared risk episodes; raw trades remain separate immutable records."""
from __future__ import annotations

import hashlib
from statistics import mean


def related(first, second, tick=.001):
    if first.get('instrument', '') != second.get('instrument', '') or first.get('side') != second.get('side'):
        return False
    risk = min(float(first.get('risk', 0)), float(second.get('risk', 0)))
    if risk <= 0:
        return False
    if abs(first['entry']-second['entry']) > max(3*tick, .5*risk):
        return False
    start = lambda p: p.get('filled_ts') or p.get('decision_ts') or p.get('placed_ts') or 0
    end = lambda p: p.get('closed_ts') or p.get('exit_deadline') or p.get('expires_ts') or 2**63-1
    return max(start(first), start(second)) <= min(end(first), end(second))


def clusters(trades, tick=.001):
    rows = sorted(trades, key=lambda p: (p.get('filled_ts') or p.get('decision_ts') or p.get('placed_ts') or 0, p['id']))
    groups = []
    for row in rows:
        touching = [group for group in groups if any(related(row, other, tick) for other in group)]
        merged = [row]
        for group in touching:
            merged += group
            groups.remove(group)
        groups.append(merged)
    result = []
    for group in groups:
        ids = sorted(p['id'] for p in group)
        result.append({'episode_id': hashlib.sha256('|'.join(ids).encode()).hexdigest()[:18],
                       'members': ids, 'trades': len(group),
                       'net_r_equal_weight': mean(p['net_r'] for p in group),
                       'decision_ts': min(p.get('decision_ts', p.get('placed_ts', 0)) for p in group),
                       'closed_ts': max(p.get('closed_ts', 0) for p in group)})
    return sorted(result, key=lambda r: (r['closed_ts'], r['episode_id']))


def summary(trades, tick=.001):
    groups = clusters(trades, tick)
    return {'raw_trades': len(trades), 'dependency_episodes': len(groups),
            'cluster_win_rate': sum(g['net_r_equal_weight'] > 0 for g in groups)/len(groups) if groups else None,
            'cluster_expectancy_r': mean(g['net_r_equal_weight'] for g in groups) if groups else None,
            'groups': groups, 'method': 'OVERLAPPING_SAME_SIDE_WITHIN_HALF_INITIAL_RISK',
            'independence_guaranteed': False,
            'pnl_weighting': 'EQUAL_WEIGHT_WITHIN_EPISODE_NOT_DEPOSIT_RETURN'}


def evidence_rows(rows):
    """One evidence weight per correlated episode, without picking its winner."""
    # Historical lean observations before v12.6 lack geometry; do not combine
    # those with newly attributed observations through a guessed distance.
    with_geometry = [r for r in rows if r.get('risk', 0) > 0 and r.get('entry')]
    others = [r for r in rows if r not in with_geometry]
    grouped = []
    for group in clusters(with_geometry):
        members = [r for r in with_geometry if r['id'] in group['members']]
        first = min(members, key=lambda r: (r['decision_ts'], r['id']))
        grouped.append({**first, 'id': group['episode_id'], 'net_r': group['net_r_equal_weight'],
                        'decision_ts': group['decision_ts'], 'closed_ts': group['closed_ts'],
                        'dependent_members': group['members']})
    return grouped + others
