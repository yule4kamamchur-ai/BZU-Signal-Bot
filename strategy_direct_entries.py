"""Build independently evidenced passive-entry anchors from completed ICT events.

These anchors are explicitly marked STRATEGY_EVENT. They are NOT claims that
one of the twenty-four original detectors fired. Geometry, HTF opposition,
real targets, costs and live-book arbitration remain with the existing engine.
"""
from __future__ import annotations

import hashlib
from typing import Optional


def proxy_setup(family: str, side: int) -> Optional[str]:
    # Legacy type is used ONLY for the shared stop/target/risk interface.
    if family in ('MICRO_SWEEP_MSS', 'LIQUIDITY_MSS'):
        return 'SWEEP_RECLAIM' if side == 1 else 'MSS_REVERSAL_SHORT'
    if family in ('MICRO_RANGE_REJECTION',):
        return 'FAILED_AUCTION_REJECTION' if side == 1 else 'FAILED_BREAKOUT_SHORT'
    if family in ('MICRO_HTF_FVG', 'HTF_FVG_RETEST'):
        return 'PULLBACK_CONTINUATION'
    if family in ('MICRO_ACCEPTED_BREAK', 'ACCEPTED_BREAK_RETEST'):
        return 'BREAKOUT_RETEST'
    return None


def from_signal(signal, snapshot, cfg, context, core, regime, playbooks):
    """A stable first-observed anchor, never a backdated executed order."""
    source = proxy_setup(signal.family, signal.side)
    if source is None or signal.side not in (1, -1):
        return None
    if not (0 <= snapshot.now - signal.signal_ts <= playbooks.STEP):
        return None
    checked = playbooks.organic.qualify(signal, snapshot, cfg.tick, regime)
    if not checked['accepted']:
        return None
    # The signal's own closed-bar invalidation must be correctly oriented.
    if signal.side * (signal.zone - signal.invalidation) <= 2 * cfg.tick:
        return None
    evidence = {
        'entry_route': 'STRATEGY_EVENT', 'planning_policy': 'DIRECT_CONFIRMED_STRATEGY',
        'setup_timeframe': '15m', 'micro_timeframe': '3m',
        'formation_timeframe': '15m', 'formation_micro_timeframe': '3m',
        'source_strategy': signal.family, 'strategy_signal_ts': signal.signal_ts,
        'strategy_event_ts': signal.event_ts, 'episode_ts': signal.event_ts,
        'strategy_regime': checked['regime'], 'strategy_evidence': signal.note,
        'setup_conditions': [
            {'condition': 'Структурна подія підтверджена закритими свічками', 'met': True},
            {'condition': 'Зона ліміту ще не відвідана', 'met': True},
            {'condition': 'Ринковий режим відповідає стратегії', 'met': True},
        ],
    }
    anchor = core.make_anchor(source, 'LONG' if signal.side == 1 else 'SHORT',
                              signal.zone, signal.invalidation, 'STRATEGY_EVENT',
                              'Незалежний ICT-сценарій: ' + signal.family,
                              context, score=min(85., checked['evidence_score'] + 9.),
                              evidence=evidence, ttl_minutes=90)
    if anchor is None:
        return None
    anchor.id = hashlib.sha256(
        f'{signal.family}|{signal.side}|{signal.signal_ts}|{signal.event_ts}|{signal.zone:.8f}'.encode()
    ).hexdigest()[:18]
    anchor.created_ts = signal.signal_ts
    anchor.expires_ts = signal.signal_ts + 90 * 60_000
    return anchor
