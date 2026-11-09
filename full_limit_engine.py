#!/usr/bin/env python3
"""Full 24-setup ICT arbitration and causal LIMIT lifecycle, v12.

No authenticated exchange orders or position sizing. A 15-minute scheduler
publishes plans; a conservative OHLC ledger tracks PAPER outcomes. Only closed
3m/15m/1H/4H candles are used. Scores are evidence rankings, not win probabilities.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import urllib.error
from bisect import bisect_left, bisect_right
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace, fields
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from statistics import mean
from typing import Any, Optional
from zoneinfo import ZoneInfo

VERSION = "full-ict-v12.32.0-organic-oil"
REPORT_RELEASE = 'v12.32.0'
PENDING_POLICY = 'ONE_DIRECTION_ONE_PENDING_MAX'
LEGACY_PENDING_POLICY = 'ONE_DIRECTION_TWO_PENDING_MAX'
import signal_quality as quality
import setup_scanner as scanner
import price_forecast
import limit_structure
import market_archive
import research_evidence
import lifecycle_forecast
import trade_dependencies
import setup_paths
import execution_atlas
import directional_policy as policy
import oil_risk
import entry_adverse_selection
import edge_intelligence
import entry_playbooks
import strategy_organics
import strategy_direct_entries
import trade_meta_model as meta
import portfolio_arbitration as arbitration
import position_monitor
import bzu_policy
import ict_methodology
SCHEMA = "full_ict_limit_state_v12"
TF = {"3m": 180_000, "15m": 900_000, "1H": 3_600_000, "4H": 14_400_000}
CANDLE_TF = {"1m":60_000, **TF}
MIN_BARS = {"3m": 50, "15m": 50, "1H": 30, "4H": 30}
ROOT = Path(__file__).resolve().parent


def iso(ts: int) -> str:
    return datetime.fromtimestamp(ts / 1000, timezone.utc).isoformat()


def ident(*parts: Any) -> str:
    return hashlib.sha256("|".join(map(str, parts)).encode()).hexdigest()[:18]


def finite(x: Any) -> float:
    value = float(x)
    if not math.isfinite(value):
        raise ValueError("Non-finite number")
    return value


@dataclass(frozen=True)
class Config:
    instrument: str = "BZ-USDT-SWAP"
    tick: float = 0.001
    maker_fee: float = 0.0002
    taker_fee: float = 0.0005
    include_commission: bool = False
    slippage_bps: float = 2.0
    min_net_rr: float = 1.80
    max_target_r: float = 2.5
    max_stop_atr: float = 4.0  # same LIMIT cap as bot_oneshot; overridden by LIMIT_MAX_STOP_ATR
    min_stop_absolute: float = .20
    max_stop_absolute: float = .75
    min_stop_atr3: float = 0.65
    min_stop_pct: float = 0.010  # legacy-plan metadata; new policy uses min_stop_absolute
    min_target_pct: float = 0.020  # legacy-plan metadata; new TP1 is 1.4R
    max_entry_distance_atr: float = 3.5
    min_score: float = 60.0
    conflict_margin: float = 8.0
    ttl_minutes: int = 120
    setup_age_minutes: int = 180
    max_hold_minutes: int = 360
    # Retained for reading old configuration/journal contracts only.
    # Daily losses and entry counts never pause or cancel a new entry.
    day_loss_cap_r: float = 0.0
    max_daily_trades: int = 0
    timezone: str = "Europe/Kyiv"
    session_end_hour: int = 22
    break_even_trigger_r: float = 1.0
    max_spread_atr: float = 0.15
    cancel_on_bias_flip: bool = True
    core: Any = field(default=None, repr=False, compare=False)
    setup_min_sample: int = 20
    analysis_scan_minutes: int = 15

    def validate(self) -> None:
        nums = config_dict(self)
        for k, v in nums.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                finite(v)
                if v < 0:
                    raise ValueError(f"Negative configuration: {k}")
        if self.tick <= 0 or self.min_net_rr <= 0 or self.max_target_r < self.min_net_rr:
            raise ValueError("Invalid tick / target geometry")
        if not 0 < self.ttl_minutes <= 180 or self.setup_age_minutes < self.ttl_minutes:
            raise ValueError("TTL must be 1..180 and no longer than setup age")
        if not 0 <= self.session_end_hour <= 23 or self.max_hold_minutes <= 0:
            raise ValueError("Invalid intraday boundaries")
        if not 0 <= self.min_score <= 100:
            raise ValueError("Invalid ranking")
        if not 0.010 <= self.min_stop_pct < 0.10 or self.min_stop_atr3 <= 0 or not 0.80 <= self.max_stop_atr <= 6.0:
            raise ValueError("Invalid stop bounds")
        if not 0.020 <= self.min_target_pct < 0.50:
            raise ValueError("TP1 must be at least 2.0% from entry")
        if self.setup_min_sample < 10:
            raise ValueError("Invalid legacy setup statistic sample")
        if self.analysis_scan_minutes not in (3,5,15):
            raise ValueError('Analysis scan interval must be 3, 5 or 15 minutes')
        if not .18 <= self.min_stop_absolute <= .22 or not .65 <= self.max_stop_absolute <= .75:
            raise ValueError("Absolute BZU stop bounds must be .18..22 and .65..75")
        if self.min_net_rr < 1.8 or self.max_entry_distance_atr > 3.5 or not 60 <= self.min_score <= 100:
            raise ValueError("Checklist requires final RR >=1.8, distance <=3.5 ATR, score >=60")
        ZoneInfo(self.timezone)

    @classmethod
    def from_env(cls) -> "Config":
        mapping = {
            "instrument": ("OKX_INST_ID", str), "tick": ("PRICE_TICK_SIZE", float),
            "maker_fee": ("MAKER_FEE_RATE", float), "taker_fee": ("TAKER_FEE_RATE", float),
            "slippage_bps": ("SLIPPAGE_BPS", float), "min_net_rr": ("MIN_NET_RR", float),
            "max_target_r": ("MAX_TARGET_R", float), "ttl_minutes": ("LIMIT_TTL_MINUTES", int),
            "setup_age_minutes": ("SETUP_AGE_MINUTES", int),
            "max_hold_minutes": ("MAX_HOLD_MINUTES", int),
            "min_stop_absolute": ("BZU_MIN_STOP_ABSOLUTE", float),
            "max_stop_absolute": ("BZU_MAX_STOP_ABSOLUTE", float),
            "max_stop_atr": ("LIMIT_MAX_STOP_ATR", float),
            "min_stop_pct": ("ABS_MIN_STOP_PCT", float),
            "min_target_pct": ("MIN_TP1_DISTANCE_PCT", float),
            "timezone": ("TRADING_TIMEZONE", str),
            "session_end_hour": ("SESSION_END_HOUR", int),
            "min_score": ("MIN_SETUP_SCORE", float),
            "max_entry_distance_atr": ("LIMIT_MAX_DISTANCE_ATR", float),
            "analysis_scan_minutes": ("ANALYSIS_SCAN_MINUTES", int),
        }
        kwargs = {k: cast(os.environ[name]) for k, (name, cast) in mapping.items()
                  if os.environ.get(name)}
        flag=os.environ.get('INCLUDE_COMMISSION','true').lower()
        if flag not in ('true','false','1','0'):
            raise ValueError('INCLUDE_COMMISSION must be true or false')
        kwargs['include_commission']=flag in ('true','1')
        if not kwargs['include_commission']:
            kwargs.update(maker_fee=0.,taker_fee=0.)
        cfg = cls(**kwargs)
        cfg.validate()
        return cfg


@dataclass(frozen=True)
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    confirmed: bool = True

    @classmethod
    def parse(cls, row: Any) -> "Candle":
        if isinstance(row, dict):
            flag = row.get("confirmed", False)
            confirmed = flag is True or str(flag) == "1"
            c = cls(int(row["ts"]), *(finite(row[k]) for k in ("open", "high", "low", "close")),
                    finite(row.get("volume", 0)), confirmed)
        else:
            if len(row) < 9:
                raise ValueError("OKX rows must include confirm flag")
            c = cls(int(row[0]), *(finite(v) for v in row[1:6]), str(row[8]) == "1")
        if c.ts < 0 or min(c.open, c.high, c.low, c.close) <= 0 or c.volume < 0:
            raise ValueError("Invalid candle values")
        if not c.low <= min(c.open, c.close) <= max(c.open, c.close) <= c.high:
            raise ValueError("Invalid OHLC ordering")
        return c


@dataclass
class Snapshot:
    now: int
    price: float
    candles: dict[str, list[Candle]]
    bid: float = 0.0
    ask: float = 0.0
    trusted: bool = True
    ticker_ts: int = 0
    instrument: str = ""
    smt_candles: list[Candle] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @classmethod
    def parse(cls, data: dict) -> "Snapshot":
        now = int(data.get("now", data.get("now_ms", 0)))
        if now <= 0:
            raise ValueError("Snapshot requires now in Unix milliseconds")
        candles = {}
        for tf, step in CANDLE_TF.items():
            rows = data.get("candles", {}).get(tf, [])
            parsed = [Candle.parse(r) for r in rows]
            # A candle's timestamp is its OPEN time, not its closing time.
            candles[tf] = sorted({c.ts: c for c in parsed if c.confirmed and c.ts + step <= now}.values(),
                                 key=lambda c: c.ts)
        # Optional archived 5m input; never a mandatory fetch/health series.
        candles['5m']=[Candle.parse(r) for r in data.get('candles',{}).get('5m',[])]
        trust = data.get("trusted", False)
        obj = cls(now, finite(data["price"]), candles, finite(data.get("bid", 0)),
                   finite(data.get("ask", 0)), trust is True,
                   int(data.get("ticker_ts", now)), str(data.get("instrument", "")))
        smt = [Candle.parse(r) for r in data.get("smt_candles_15m", [])]
        obj.smt_candles = sorted({c.ts:c for c in smt if c.confirmed and c.ts+TF["15m"]<=now}.values(),
                                key=lambda c:c.ts)
        obj.metadata = {k: v for k, v in data.items() if k not in ("candles", "smt_candles_15m")}
        # Reuse already fetched minutes; the one-shot/report cadence is intact.
        obj.candles['5m']=scanner.closed_five_minutes(obj)
        return obj


def config_dict(cfg: Config) -> dict:
    values={f.name: getattr(cfg, f.name) for f in fields(cfg) if f.name != "core"}
    if not cfg.include_commission:
        values.update(maker_fee=0.,taker_fee=0.)
    return values


@contextmanager
def market_clock(core, ts: int):
    old = core.now_utc
    core.now_utc = lambda: datetime.fromtimestamp(ts/1000, timezone.utc)
    try:
        yield
    finally:
        core.now_utc = old


def round_tick(value: float, tick: float, up: bool = False) -> float:
    q = Decimal(str(value)) / Decimal(str(tick))
    return float(q.to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR) * Decimal(str(tick)))


def atr(rows: list[Candle], period: int = 14) -> float:
    if len(rows) < 2:
        return 0.0
    tr = [max(c.high - c.low, abs(c.high - p.close), abs(c.low - p.close))
          for p, c in zip(rows, rows[1:])]
    return mean(tr[-period:])


def pivots(rows: list[Candle], strength: int = 2) -> list[dict]:
    """Pivots carry availability time; all right-hand bars must already exist."""
    found = []
    for i in range(strength, len(rows) - strength):
        others = rows[i-strength:i] + rows[i+1:i+strength+1]
        for kind, yes, level in (
            ("HIGH", all(rows[i].high > x.high for x in others), rows[i].high),
            ("LOW", all(rows[i].low < x.low for x in others), rows[i].low),
        ):
            if yes:
                found.append({"kind": kind, "level": level, "ts": rows[i].ts,
                              "known_ts": rows[i+strength].ts})
    return found


def bias(rows: list[Candle]) -> dict:
    points = pivots(rows[-60:])
    highs = [p["level"] for p in points if p["kind"] == "HIGH"]
    lows = [p["level"] for p in points if p["kind"] == "LOW"]
    side = 0
    if len(highs) >= 2 and len(lows) >= 2:
        if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
            side = 1
        elif highs[-1] < highs[-2] and lows[-1] < lows[-2]:
            side = -1
    # A closed structural break overrides older swing classification.
    if rows and len(highs) >= 1 and len(lows) >= 1:
        if rows[-1].close > highs[-1]:
            side = 1
        elif rows[-1].close < lows[-1]:
            side = -1
    window = rows[-40:]
    low = min((c.low for c in window), default=0.0)
    high = max((c.high for c in window), default=0.0)
    return {"side": side, "high": high, "low": low, "mid": (high+low)/2,
            "pivots": points, "status": "TREND" if side else "RANGE_OR_TRANSITION"}


def session_cutoff(ts: int, cfg: Config) -> int:
    local = datetime.fromtimestamp(ts/1000, ZoneInfo(cfg.timezone))
    end = local.replace(hour=cfg.session_end_hour, minute=0, second=0, microsecond=0)
    return int(end.timestamp()*1000)


def health(s: Snapshot, cfg: Config) -> list[str]:
    errors = []
    if not s.trusted or s.price <= 0:
        errors.append("UNTRUSTED_PRICE")
    if s.instrument and s.instrument != cfg.instrument:
        errors.append("WRONG_INSTRUMENT")
    if s.ticker_ts > s.now or s.now - s.ticker_ts > 120_000:
        errors.append("STALE_TICKER")
    for tf, step in CANDLE_TF.items():
        rows = s.candles.get(tf, [])
        if tf == "1m":
            if s.metadata.get("require_1m") and (len(rows)<15 or s.now-(rows[-1].ts+step)>120_000):
                errors.append("INSUFFICIENT_1m")
            if any(b.ts-a.ts!=step for a,b in zip(rows[-15:-1],rows[-14:])):
                errors.append("GAP_1m")
            continue
        if len(rows) < MIN_BARS[tf]:
            errors.append(f"INSUFFICIENT_{tf}")
            continue
        if s.now - (rows[-1].ts+step) > step+60_000:
            errors.append(f"STALE_{tf}")
        if any(b.ts-a.ts != step for a, b in zip(rows[-MIN_BARS[tf]:], rows[-MIN_BARS[tf]+1:])):
            errors.append(f"GAP_{tf}")
    if s.bid <= 0 or s.ask < s.bid:
        errors.append("MISSING_OR_INVALID_SPREAD")
    elif atr(s.candles.get("15m", [])) > 0:
        if (s.ask-s.bid)/atr(s.candles["15m"]) > cfg.max_spread_atr:
            pass  # Confluence/fees diagnostics; no separate spread veto.
    return errors










def fees_r(entry: float, exit_price: float, risk: float, cfg: Config) -> float:
    # Take-profit conservatively assumes taker fees too. Rates must match account.
    return (entry*fee_rate(cfg,'maker') + exit_price*fee_rate(cfg,'taker'))/risk


def fee_rate(cfg: Config, kind: str) -> float:
    return getattr(cfg,kind+'_fee') if cfg.include_commission else 0.


def execution_config(plan: dict, cfg: Config) -> Config:
    """Freeze entry-era costs, including existing no-commission orders."""
    included = plan.get('include_commission', cfg.include_commission)
    if (included==cfg.include_commission
            and plan.get('entry_fee_rate',cfg.maker_fee)==cfg.maker_fee
            and plan.get('exit_fee_rate',cfg.taker_fee)==cfg.taker_fee
            and plan.get('slippage_bps',cfg.slippage_bps)==cfg.slippage_bps):
        return cfg
    return replace(cfg, include_commission=included,
                   maker_fee=plan.get('entry_fee_rate',cfg.maker_fee) if included else 0.,
                   taker_fee=plan.get('exit_fee_rate',cfg.taker_fee) if included else 0.,
                   slippage_bps=plan.get('slippage_bps',cfg.slippage_bps))


REVERSALS = quality.COUNTERTREND_SETUPS


def setup_statistics(state: dict, cfg: Config) -> dict:
    """Only this execution architecture can calibrate its own setup ranking."""
    report = {}
    for setup in cfg.core.DETECTED_SETUP_TYPES:
        rows = [t for t in state.get("trades", []) if t.get("setup_type") == setup
                and t.get("bot_version_at_entry",t.get("entry_version")) == VERSION]
        stat = statistics(rows)
        n = stat["trades"]
        status = "INSUFFICIENT_SAMPLE" if n < cfg.setup_min_sample else (
            "NEGATIVE_EXPECTANCY" if stat["expectancy_r"] <= 0 else "POSITIVE_EXPECTANCY")
        report[setup] = {**stat, "status": status, "required_sample": cfg.setup_min_sample,
                         "version": VERSION, "probability_validated": False}
        report[setup]['timeframes']={}
        for tf in scanner.PRIMARY:
            subset=statistics([t for t in rows if scanner.timeframe(t)==tf])
            subset['status']='INSUFFICIENT_SAMPLE' if subset['trades']<cfg.setup_min_sample else (
                'NEGATIVE_EXPECTANCY' if subset['expectancy_r']<=0 else 'POSITIVE_EXPECTANCY')
            report[setup]['timeframes'][tf]=subset
        report[setup]['formation_timeframes']={tf:statistics([t for t in rows if t.get('formation_timeframe',scanner.timeframe(t))==tf])
                                               for tf in ('3m',*scanner.PRIMARY)}
    return report


def build_full_context(s: Snapshot, state: dict, cfg: Config) -> dict:
    core = cfg.core
    # SMT must compare the same 30 closed intervals, never a stale peer series.
    expected = [c.ts for c in s.candles["15m"][-30:]]
    peers = {c.ts:c for c in s.smt_candles}
    smt_aligned = len(expected)==30 and all(ts in peers for ts in expected)
    smt_rows = [peers[ts] for ts in expected] if smt_aligned else []
    data = {**s.metadata, "price": s.price, "spread": s.ask-s.bid,
            "execution_price_trusted": s.trusted, "ticker_ts": s.ticker_ts,
            "instrument": cfg.instrument, "instrument_label": core.INSTRUMENT_LABEL,
            "price_source": s.metadata.get("price_source", "CLOSED_CANDLE_SNAPSHOT"),
            "candles": s.candles, "smt_candles_15m": smt_rows,
            "htf_source": {tf: "CONFIRMED_SNAPSHOT" for tf in ("1H", "4H")}}
    ctx = core.build_context(data, state, {"trades": state.get("trades", [])})
    ctx["smt"]["data_status"] = "ALIGNED_30_CLOSED_BARS" if smt_aligned else "UNAVAILABLE_OR_MISALIGNED"
    return ctx


def anchor_zone_key(a, cfg: Config) -> str:
    tf = a.evidence.get('setup_timeframe', '15m')
    parts = (a.setup_type, a.side, round(a.level/cfg.tick), round(a.invalidation/cfg.tick), a.kind)
    if a.evidence.get('entry_route')=='MICRO_PRECURSOR':
        return ident(*parts,tf,'FORMATION_3m')
    if a.evidence.get('entry_route')=='STRATEGY_EVENT':
        return ident(*parts,tf,'STRATEGY_EVENT',a.evidence.get('source_strategy'))
    return ident(*parts) if tf == '15m' else ident(*parts, tf)


def anchor_key(a, cfg: Config) -> str:
    zone=anchor_zone_key(a,cfg)
    episode=a.evidence.get('episode_ts')
    return ident(zone,episode) if episode else zone


def merge_full_anchors(fresh: list, state: dict, s: Snapshot, cfg: Config, contexts=None) -> list:
    """Stable first observation per episode; renewal needs new market evidence."""
    core = cfg.core
    memory = state.setdefault("anchor_memory", {})
    live_zones=set()
    for plan in open_plans(state):
        raw=memory.get(plan.get('event_key'),{})
        live_zones.add(plan.get('zone_key') or raw.get('evidence',{}).get('zone_identity'))
        if raw.get('evidence',{}).get('zone_identity'):
            live_zones.add(raw['evidence']['zone_identity'])
    by_zone={}
    for raw in memory.values():
        previous=core.anchor_from_dict(raw)
        if previous is not None:
            zone=raw.get('evidence',{}).get('zone_identity') or anchor_zone_key(previous,cfg)
            if zone not in by_zone or raw['created_ts']>by_zone[zone]['created_ts']:
                by_zone[zone]=raw
    for a in fresh:
        zone=anchor_zone_key(a,cfg)
        previous=by_zone.get(zone)
        event=None
        organic = (a.evidence.get('source_strategy') in scanner.ORGANIC_FORMATIONS
                   or bool((a.evidence.get('entry_zone') or {}).get('wave_proof')))
        if organic and previous and previous.get('evidence', {}).get('episode_ts') != a.evidence['episode_ts']:
            previous = None
        if previous and not organic and zone not in live_zones and contexts:
            native=contexts.get('MICRO_PRECURSOR' if a.evidence.get('entry_route')=='MICRO_PRECURSOR' else a.evidence.get('setup_timeframe','15m'),{})
            sign=core.side_sign(a.side)
            visited=any(c.low<=a.level if sign==1 else c.high>=a.level
                        for c in limit_structure.closed(s,'3m') if c.ts>=previous['created_ts'])
            epoch=limit_structure.session_epoch(a,native)
            if previous['created_ts']<epoch<=s.now:
                event=limit_structure.renewal_event(a,previous,native,s,cfg)
            elif s.now>=previous['expires_ts']:
                event=limit_structure.renewal_event(a,previous,native,s,cfg)
            elif visited or previous.get('state') in ('INVALIDATED','REJECTED','TRIGGERED'):
                event=limit_structure.renewal_event(a,previous,native,s,cfg,after=previous['created_ts'])
        if previous:
            if event:
                a.evidence.update(episode_ts=event['known_ts'],episode_kind=event['kind'])
                a.id=ident(zone,event['known_ts'])
                a.state='ARMED';a.touched=False;a.touched_ts=0
                a.created_ts=s.now
                state.setdefault('setup_renewals',[]).append({'setup':a.setup_type,
                    'timeframe':a.evidence.get('setup_timeframe','15m'),'level':a.level,
                    'decision_ts':s.now,'known_ts':event['known_ts'],'kind':event['kind']})
                state['setup_renewals']=state['setup_renewals'][-200:]
            else:
                a.id = previous["id"]
                a.created_ts = previous["created_ts"]
                a.expires_ts = previous["expires_ts"]
                a.cooldown_until_ts = previous.get("cooldown_until_ts", 0)
                for name in ('state','touched','touched_ts','last_checked_ts','reject_reason'):
                    if name in previous:
                        setattr(a,name,previous[name])
                for name in ('activation_policy','activation_phase','break_known_ts','break_observed_ts','original_created_ts','episode_ts','episode_kind'):
                    if name in previous.get('evidence',{}):
                        a.evidence[name]=previous['evidence'][name]
        a.evidence['zone_identity']=zone
        key=anchor_key(a,cfg)
        a.expires_ts = min(a.expires_ts, a.created_ts+cfg.setup_age_minutes*scanner.MINUTES[a.evidence.get("setup_timeframe","15m")]//15*60_000)
        memory[key] = core.anchor_to_dict(a)
        by_zone[zone]=memory[key]
    # Retain tombstones beyond setup lifetime to prevent a detector refreshing age.
    memory = {k: v for k, v in memory.items() if s.now-v["created_ts"] <= 7*86_400_000}
    for key,raw in list(memory.items()):
        a=core.anchor_from_dict(raw)
        if a is None or not (a.created_ts<=s.now<a.expires_ts):continue
        native=(contexts or {}).get('MICRO_PRECURSOR' if a.evidence.get('entry_route')=='MICRO_PRECURSOR' else a.evidence.get('setup_timeframe','15m'),{})
        if (a.evidence.get('source_strategy') in scanner.ORGANIC_FORMATIONS
                or (a.evidence.get('entry_zone') or {}).get('wave_proof')):
            continue
        if a.setup_type in setup_paths.BREAK_RETEST and 'activation_phase' not in a.evidence:
            if key not in {p['event_key'] for p in open_plans(state)}:
                a.evidence.update(activation_phase='WAIT_BREAK',activation_policy='CLOSED_BREAK_THEN_LIMIT_RETEST')
                memory[key]=core.anchor_to_dict(a)
        if setup_paths.activate(a,native,s,cfg,observed_after=state.get('last_run_ts',0)):
            memory.pop(key)
            memory[anchor_key(a,cfg)]=core.anchor_to_dict(a)
    state["anchor_memory"] = memory
    return [a for raw in memory.values() for a in [core.anchor_from_dict(raw)] if a is not None
            and a.created_ts <= s.now < a.expires_ts
            and a.state not in {"TRIGGERED", "INVALIDATED", "REJECTED"}]


def retire_distant_anchor(state: dict, anchor, key: str, snapshot: Snapshot, cfg: Config) -> None:
    """Tombstone stale candidates; never move the original passive limit.

    Native detector observations can appear repeatedly in later scans. Simply
    dropping the rejected plan repeats the same LIMIT_UNREACHABLE forever.
    Record the old *event* as rejected, allowing a subsequent independent BOS,
    FVG, session event or verified structural renewal to generate a NEW key.
    Existing pending positions are left to the lifecycle manager.
    """
    ledger = state.setdefault('anchor_memory', {})
    if key not in ledger:
        return
    current = dict(ledger[key])
    if current.get('state') in ('TRIGGERED', 'INVALIDATED', 'REJECTED'):
        return
    current['state'] = 'REJECTED'
    current['reject_reason'] = 'LIMIT_UNREACHABLE_REQUIRES_NEW_STRUCTURE'
    current['last_checked_ts'] = snapshot.now
    ledger[key] = current
    audit = state.setdefault('stale_refresh', [])
    audit.append({'ts': snapshot.now, 'event_key': key,
                  'setup': anchor.setup_type, 'side': cfg.core.side_sign(anchor.side),
                  'level': anchor.level, 'reason': 'LIMIT_UNREACHABLE',
                  'next_action': 'FRESH_CONFIRMED_BOS_FVG_OR_RENEWAL'})
    state['stale_refresh'] = audit[-100:]


def full_targets(ctx: dict, s: Snapshot, side: str, entry: float, cfg: Config) -> list[dict]:
    return limit_structure.structural_targets(ctx,s,side,entry,cfg,sys.modules[__name__])


def stop_geometry(a, ctx, cfg):
    """Shared structural/volatility stop with the user's absolute floor."""
    sign=cfg.core.side_sign(a.side)
    entry=round_tick(a.level,cfg.tick,up=sign==-1)
    buffer=max(.15*ctx['atr3'],2*cfg.tick)
    structural=round_tick(a.invalidation-sign*buffer,cfg.tick,up=sign==-1)
    floor=max(cfg.min_stop_pct*entry,cfg.min_stop_atr3*ctx['atr3'],4*cfg.tick)
    stop=round_tick(entry-sign*max(sign*(entry-structural),floor),cfg.tick,up=sign==-1)
    return entry,stop,sign*(entry-stop),floor,buffer


def target_geometry(entry, stop, risk, buffer, side, targets, cfg):
    """Choose a fresh destination; respect strong unmitigated path barriers."""
    required_distance=max(cfg.min_target_pct*entry,cfg.min_net_rr*risk)
    required_tp=round_tick(entry+side*required_distance,cfg.tick,up=side==1)
    geometry={'entry':entry,'stop':stop,'initial_stop':stop,'risk':risk,
              'required_rr':cfg.min_net_rr,'required_tp':required_tp,
              'min_target_pct':cfg.min_target_pct,'target_distance_met':False}
    if not targets:
        return geometry
    chosen,intermediate,barrier=limit_structure.choose_target(targets,entry,risk,buffer,side,cfg)
    nearest=barrier or chosen
    target=round_tick(entry+side*min(nearest['distance']-buffer,cfg.max_target_r*risk),
                      cfg.tick,up=side==-1)
    distance=side*(target-entry)
    distance_met=distance+1e-9>=cfg.min_target_pct*entry
    geometry.update(available_tp1=target,gross_rr=distance/risk,
                    tp1_distance_pct=distance/entry*100,target_distance_met=distance_met,
                    target_source=nearest['kind'],obstacle_level=nearest['level'],
                    chosen_target=chosen,intermediate_levels=intermediate,
                    path_barrier=barrier,path_clear=barrier is None)
    if distance_met and barrier is None:
        geometry.update(tp1=target,tp=target)
    return geometry


def preview_geometry(a, ctx, s, cfg):
    """Hypothetical entry geometry for a blocked quote; never grants admission.

    All levels come from the same structural stop and actual target rules as
    full_plan. This lets the price forecast explain a forming/blocked entry.
    """
    sign=cfg.core.side_sign(a.side)
    if not sign or min(ctx['atr3'],ctx['atr15'])<=0:
        return {}
    entry,stop,risk,floor,buffer=stop_geometry(a,ctx,cfg)
    if sign*(entry-a.invalidation)<=0:
        return {}
    targets=full_targets(ctx,s,a.side,entry,cfg)
    geometry=target_geometry(entry,stop,risk,buffer,sign,targets,cfg)
    tf=a.evidence.get('setup_timeframe','15m')
    geometry.update(geometry_preview=True,
                    max_hold_minutes=cfg.max_hold_minutes*scanner.MINUTES[tf]//15,intraday=tf=='15m')
    return geometry


def full_plan(a, ctx: dict, s: Snapshot, cfg: Config, stats: dict, diagnostics=None):
    return bzu_policy.plan(a,ctx,s,cfg,stats,sys.modules[__name__],diagnostics)


def _historical_anchor_index(memory: dict) -> tuple[set[str], dict[tuple, list[float]]]:
    """Index past zone identities and levels once per cycle, without pruning them.

    The old implementation scanned all saved anchors for EVERY new candidate.
    That becomes very expensive after thousands of 15-minute cycles. The index
    preserves the exact less-than-half-tick match used for duplicate detection.
    """
    zones: set[str] = set()
    levels: dict[tuple, list[float]] = {}
    for old in memory.values():
        if not isinstance(old, dict):
            continue
        evidence = old.get('evidence') or {}
        zone = evidence.get('zone_identity')
        if zone:
            zones.add(zone)
        group = (old.get('setup_type'), old.get('side'),
                 evidence.get('setup_timeframe', '15m'),
                 evidence.get('entry_route', 'NATIVE_PATTERN'))
        level = old.get('level', 0)
        if isinstance(level, (int, float)) and math.isfinite(level):
            levels.setdefault(group, []).append(float(level))
    for values in levels.values():
        values.sort()
    return zones, levels


def _anchor_already_seen(index: tuple, candidate: Any, timeframe: str,
                         zone_identity: str, tick: float) -> bool:
    zones, levels = index
    if zone_identity in zones:
        return True
    evidence = candidate.evidence
    group = (candidate.setup_type, candidate.side, timeframe,
             evidence.get('entry_route', 'NATIVE_PATTERN'))
    values = levels.get(group, ())
    if not values:
        return False
    position = bisect_left(values, candidate.level)
    for i in (position-1, position):
        if 0 <= i < len(values) and abs(values[i] - candidate.level) < tick/2:
            return True
    return False


def analyze(s: Snapshot, cfg: Config, state: dict) -> dict:
    errors = health(s,cfg)
    if errors:
        return {"health": errors,"plans": [],"watch": [],"rejections": {},"contexts": {}}
    core = cfg.core
    if core is None:
        raise ValueError("Full 24-setup core required")
    if len(core.DETECTORS) != 24 or len({d.__name__ for d in core.DETECTORS}) != 24:
        raise ValueError("The complete registry of 24 original detectors is required")
    with market_clock(core,s.now):
        ctx = build_full_context(s,state,cfg)
        tf_contexts = scanner.build_contexts(s,ctx,state,cfg)
        tf_contexts={tf:execution_atlas.prepare(native,s,cfg,sys.modules[__name__])
                     for tf,native in tf_contexts.items()}
        early_context=execution_atlas.micro_context(s,tf_contexts['15m'],state,cfg,sys.modules[__name__])
        state["regime_memory"] = copy.deepcopy(ctx.get("regime_memory", {}))
        fresh, detector_audit, matrix = [], [], []
        # The registry order is stable; each original setup runs on native
        # 15m, 1H and 4H bars, not merely on a 15m trend with HTF labels.
        historical_anchor_index = _historical_anchor_index(state.get('anchor_memory', {}))
        names = [row['setup'] for row in core_setup_registry(core)]
        for setup,detector in zip(names,core.DETECTORS):
            evaluations=[]
            for tf,native in tf_contexts.items():
                try:
                    alternatives=setup_paths.candidates(detector,setup,native,core)
                    micro_candidates=[]
                    if tf=='15m':
                        micro_candidates=setup_paths.candidates(detector,setup,early_context,core)
                        for early in micro_candidates:
                            early.evidence['entry_route']='MICRO_PRECURSOR'
                            early.evidence['formation_timeframe']='3m'
                            early.expires_ts=min(early.expires_ts,early.created_ts+45*60_000)
                        alternatives+=micro_candidates
                    a=max(alternatives,key=lambda item:(
                        core.side_sign(item.side)*(s.price-item.level)>0,item.score),default=None)
                    row = scanner.conditions(setup,native,core,a)
                    row['setup_conditions']=copy.deepcopy(row.get('conditions',[]))
                    row['setup_readiness_pct']=row.get('readiness_pct',0)
                    scanner.update_readiness(row)
                    for candidate in alternatives:
                        candidate=copy.deepcopy(candidate)
                        candidate.evidence['setup_timeframe']=tf
                        candidate.evidence['micro_timeframe']=scanner.MICRO[tf]
                        formation=early_context if candidate.evidence.get('entry_route')=='MICRO_PRECURSOR' else native
                        candidate.evidence['setup_conditions']=copy.deepcopy(scanner.conditions(setup,formation,core,candidate)['conditions'])
                        if candidate.evidence.get('planning_policy')=='PREMAPPED_RANGE_EDGE':
                            candidate.evidence['setup_conditions'][-1]={'condition':'Межа визначена до підходу ціни; до 3 ATR','met':True}
                        candidate.expires_ts=candidate.created_ts+(candidate.expires_ts-candidate.created_ts)*scanner.MINUTES[tf]//15
                        seen_zone=anchor_zone_key(candidate,cfg)
                        known=_anchor_already_seen(historical_anchor_index,candidate,tf,seen_zone,cfg.tick)
                        if not known:
                            setup_paths.activate(candidate,formation,s,cfg,initial=True)
                        fresh.append(candidate)
                    if a is not None:
                        a=next((c for c in fresh[::-1] if c.setup_type==setup and c.side==a.side and c.level==a.level),a)
                    row.update(timeframe=tf,reason='ACCEPTED' if a else 'SETUP_CONDITIONS_INCOMPLETE',
                               score=a.score if a else 0,score_status='PATTERN' if a else 'UNAVAILABLE',anchor_id=a.id if a else None)
                    row['event_key']=anchor_key(a,cfg) if a else None
                    row.update(scanner.proximity(s.price,row.get('level')))
                    matrix.append(row)
                    evaluations.append({'timeframe':tf,'status':'DETECTED' if a else 'NO_SETUP',
                        'branches':len(alternatives),'sides':sorted({candidate.side for candidate in alternatives}),
                        'native_status':'DETECTED' if len(alternatives)>len(micro_candidates) else 'NO_SETUP',
                        'native_branches':len(alternatives)-len(micro_candidates),
                        'micro_precursors':len(micro_candidates)})
                except Exception as exc:
                    row={'setup':setup,'timeframe':tf,'status':'ERROR','reason':'DETECTOR_ERROR',
                         'error':type(exc).__name__,'readiness_pct':None,'score':0,
                         'missing_conditions':['Помилка аналізу сетапу'],'level':None,'side':0}
                    matrix.append(row)
                    evaluations.append({'timeframe':tf,'status':'ERROR','error':type(exc).__name__})
            status='ERROR' if any(e['status']=='ERROR' for e in evaluations) else 'DETECTED' if any(e['status']=='DETECTED' for e in evaluations) else 'NO_SETUP'
            detector_audit.append({'detector':detector.__name__,'setup':setup,'status':status,
                                   'timeframes':evaluations})
        # Independent strategy-confirmed entries must not require an accidental
        # price match with one of the legacy 24 location detectors. Use the
        # SAME shared geometry/target/order machinery, with explicit origin.
        organic_regime = strategy_organics.classify(s)
        active_side = policy.active_structure(s, cfg.tick, sys.modules[__name__]).get('side', 0)
        organic_setups = entry_playbooks.scenarios_since(
            s, cfg.tick, state.get('last_run_ts', 0))
        direct_anchors = []
        for signal in organic_setups:
            # Direct BOS/FVG confirms on CLOSED 3m candles, but its trade is
            # planned on the TRUE 15m structure. The micro precursor context
            # relabels 3m bars as 15m for legacy detectors: using it here
            # erroneously applied the 4*ATR stop cap to 3m ATR and missed
            # genuine 15m obstacles/targets. Keep these contracts separate.
            a = strategy_direct_entries.from_signal(signal,s,cfg,tf_contexts['15m'],
                                                      core,organic_regime,entry_playbooks,active_side)
            if a is not None:
                direct_anchors.append(a)
        direct_anchors.extend(scanner.organic_anchors(s,cfg,tf_contexts['15m'],sys.modules[__name__]))
        fresh.extend(direct_anchors)
        fresh.extend(bzu_policy.session_anchors(s,tf_contexts["15m"],cfg,sys.modules[__name__]))
        anchors = merge_full_anchors(fresh,state,s,cfg,{**tf_contexts,'MICRO_PRECURSOR':early_context})
        stats = setup_statistics(state,cfg)
        plans, watch, refused = [], [], {}
        admission_paths=[]
        active_keys={anchor_key(a,cfg) for a in anchors}
        for a in fresh:
            row=(None if a.evidence.get('entry_route')=='STRATEGY_EVENT' else
                 next((r for r in matrix if r['setup']==a.setup_type and r['timeframe']==a.evidence.get('setup_timeframe','15m')),None))
            if row is None:
                continue
            row['event_key']=anchor_key(a,cfg)
            if row['event_key'] not in active_keys:
                reason='ANCHOR_EXPIRED_AWAITING_NEW_EVENT' if s.now>=a.expires_ts else 'ANCHOR_INVALIDATED'
                row.update(status='BLOCKED',reason=reason,
                           expired_ts=a.expires_ts,entry_checks=scanner.entry_checks())
                next(c for c in row['entry_checks'] if c['key']=='FRESH_LEVEL')['met']=False
                scanner.update_readiness(row)
                refused[reason]=refused.get(reason,0)+1
        for a in anchors:
            tf=a.evidence.get('setup_timeframe','15m')
            route=a.evidence.get('entry_route')
            native=early_context if route=='MICRO_PRECURSOR' else tf_contexts[tf]
            key = anchor_key(a,cfg)
            level_key = ident(a.side,round(a.level/cfg.tick))
            diagnostics={}
            if key in state.get("used_events",{}) or state.get("level_cooldown",{}).get(level_key,0)>s.now:
                p, reason = None, "LEVEL_OR_EVENT_COOLDOWN"
            elif (a.setup_type=='CAPITULATION_RECOVERY'
                  and not setup_paths.capitulation_recovery(native,core)['confirmed']):
                p,reason=None,'CAPITULATION_RECOVERY_NOT_CONFIRMED'
            else:
                if a.setup_type=='CAPITULATION_RECOVERY':
                    a.evidence['recovery_proof']=setup_paths.capitulation_recovery(native,core)
                p, reason = full_plan(a,native,s,cfg,stats,diagnostics)
                zone=diagnostics.get('entry_zone')
                if zone:
                    zone_id=ident(a.side,zone['low'],zone['high'],zone['known_ts'],zone['timeframe'])
                    spent=state.setdefault('mitigated_zones',{})
                    if reason=='ZONE_MITIGATED':
                        spent[zone_id]={'known_ts':zone['known_ts'],'observed_ts':s.now,'state':'MITIGATED'}
                    if zone_id in spent:
                        p,reason=None,'ZONE_MITIGATED'

                if not p and reason == 'LIMIT_UNREACHABLE':
                    retire_distant_anchor(state, a, key, s, cfg)
                    diagnostics['refresh_policy'] = 'NEW_CONFIRMED_STRUCTURE_ONLY'
                if not p and 'entry' not in diagnostics and reason in ('LIMIT_WOULD_CROSS_SPREAD','LIMIT_UNREACHABLE'):
                    diagnostics.update(preview_geometry(a,native,s,cfg))
            admission_paths.append({'setup':a.setup_type,'timeframe':tf,'side':core.side_sign(a.side),
                'entry':diagnostics.get('entry', a.level),'reason':reason,'event_key':key,
                'entry_source':'PLANNER' if 'entry' in diagnostics else 'ANCHOR',
                **{k:diagnostics[k] for k in ('stop','risk','tp1','tp2','approach','status') if k in diagnostics},
                'route':a.evidence.get('entry_route','NATIVE_PATTERN'),
                'source_strategy':a.evidence.get('source_strategy'),
                'entry_zone':diagnostics.get('entry_zone') or a.evidence.get('entry_zone'),
                'score':p.get('score') if p else a.score,
                'score_status':'CONTEXT' if p else 'PATTERN'})
            row=(None if a.evidence.get('entry_route')=='STRATEGY_EVENT' else
                 next((r for r in matrix if r['setup']==a.setup_type and r['timeframe']==tf),None))
            if row is not None:
                row.setdefault('branch_results',[]).append({'side':core.side_sign(a.side),'level':a.level,'reason':reason,'event_key':key})
            replace_row=row is not None and (row.get('status')!='LIMIT_READY' or (p is not None and p['score']>row.get('score',0)))
            if replace_row:
                row.update(level=a.level,side=core.side_sign(a.side),anchor_id=a.id,
                           reason=reason,score=p['score'] if p else a.score,
                           status='LIMIT_READY' if p else 'BLOCKED',
                           signal_id=p['id'] if p else None,event_key=key)
                scanner.replace_plan_diagnostics(row, diagnostics)
                row['score_status']='CONTEXT' if p or 'score' in diagnostics else 'PATTERN'
                if a.evidence.get('setup_conditions'):
                    row['setup_conditions']=copy.deepcopy(a.evidence['setup_conditions'])
                elif row.get('setup_readiness_pct',0)<100:
                    row['setup_conditions']=[{'condition':'Патерн підтверджено при формуванні рівня','met':True}]
                scanner.update_readiness(row)
                row.update(scanner.proximity(s.price,a.level))
            if p:
                p["setup_conditions"]=copy.deepcopy(a.evidence.get("setup_conditions",[]))
                p["level_key"] = level_key
                plans.append(p)
            else:
                refused[reason] = refused.get(reason,0)+1
                watch.append({"setup": a.setup_type,"timeframe":tf,"readiness_pct":100.,
                              "anchor_id": a.id,"side": core.side_sign(a.side),
                              "level": a.level,"reason": reason,"score": a.score})
    if set(quality.CONTRACTS)!=set(core.DETECTED_SETUP_TYPES):
        raise ValueError('Quality contracts must cover all 24 original setups')
    entry_playbooks.validate_contracts(core.DETECTED_SETUP_TYPES)
    ledger = quality.ensure(state,cfg)
    # An independent model is trained only on fully resolved past shadow fills.
    # Failure/insufficient data => monitoring only, never a made-up P(win).
    meta_model = meta.train(ledger['observations'],s.now,quality.STRATEGY)
    ledger['last_meta_diagnostic']={key:value for key,value in meta_model.items()
                                    if key not in ('weights','calibration_offset')}
    # Evaluate every scenario BEFORE combining nearby levels. A high-score
    # unconfirmed continuation must not suppress a valid reversal at that price.
    assessments = {p['id']:quality.assess(p,s,early_context if p.get('entry_route')=='MICRO_PRECURSOR' else tf_contexts[scanner.timeframe(p)],cfg,
                        sys.modules[__name__],ledger['observations']) for p in plans}
    # Three independent, regime-specific strategy hypotheses are calculated ONCE
    # per snapshot, not again for every legacy-detector candidate. No future bars.
    for p in plans:
        q = assessments[p['id']]
        proof=policy.evidence(p,s,cfg.tick)
        sweep = p.get('zone_origin', {}).get('sweep')
        if sweep and not proof['sweep_reclaim']:
            proof.update(sweep_reclaim=True, latest_sweep=sweep)
        p['ict_evidence']=proof
        if p.get('entry_route')=='STRATEGY_EVENT':
            # A direct event is evaluated against its exact as-of strategy
            # evidence below, not misrepresented as a legacy detector hit.
            src=p.get('anchor_proof',{})
            admitted=bool(src.get('source_strategy') and
                          src.get('strategy_signal_ts') is not None and
                          src.get('strategy_event_ts') is not None)
            if p.get('policy') == bzu_policy.POLICY:
                admitted = admitted and (p.get('zone_origin', {}).get('wave_confirmed') or
                                         ict_methodology.entry_trigger(p, proof))
            ict_reason='DIRECT_STRATEGY_EVENT' if admitted else 'DIRECT_STRATEGY_PROOF_MISSING'
            p['ict_contract']={'source':'STRATEGY_EVENT', 'source_strategy':src.get('source_strategy'),
                               'signal_ts':src.get('strategy_signal_ts'),
                               'event_ts':src.get('strategy_event_ts')}
        else:
            admitted,ict_reason=policy.admit(p,proof)
        if q['accepted'] and not admitted:
            q={**q,'accepted':False,'reason':ict_reason}
            assessments[p['id']]=q
        # A holdout-validated meta model may veto a low-quality executable
        # plan; never train on the current plan's unknown future outcome.
        meta_decision=meta.evaluate(meta_model,q['features'],
                                    net_rr=p.get('net_rr',0),costs_r=p.get('costs_r',0))
        p['meta_decision']=meta_decision
        # Independent regime and strategy-family control. SHADOW mode is the
        # local/test default; production explicitly opts into ENFORCE. Do not
        # touch pre-existing positions or pending orders during an upgrade.
        edge_verdict=edge_intelligence.assess(p,s,proof)
        p['edge_intelligence']=edge_verdict
        edge_mode=os.environ.get('BZU_EDGE_MODE','SHADOW').strip().upper()
        if edge_mode not in ('SHADOW','ENFORCE','RESEARCH_ONLY'):
            raise ValueError('BZU_EDGE_MODE must be SHADOW, ENFORCE, or RESEARCH_ONLY')
        if q['accepted'] and edge_mode=='RESEARCH_ONLY':
            q={**q,'accepted':False,'reason':'EDGE_RESEARCH_NOT_VALIDATED'}
            assessments[p['id']]=q
        if q['accepted'] and not bzu_policy.is_new(p) and edge_mode=='ENFORCE' and edge_verdict['status']!='ALLOW':
            q={**q,'accepted':False,'reason':edge_verdict['reason']}
            assessments[p['id']]=q
        # v12.17: original 24 detectors locate zones; a separate, ordered
        # early causal 3m/15m event-based playbook must independently justify the LIMIT entry.
        # This is strategy selection, NOT a portfolio score or a pretend P(win).
        playbook_mode=os.environ.get('BZU_ENTRY_PLAYBOOK_MODE','SHADOW').strip().upper()
        if playbook_mode not in ('SHADOW','STRICT'):
            raise ValueError('BZU_ENTRY_PLAYBOOK_MODE must be SHADOW or STRICT')
        playbook=entry_playbooks.assess(p,s,cfg.tick,setup_candidates=organic_setups,market_regime=organic_regime)
        p['entry_playbook']=playbook
        if not bzu_policy.is_new(p) and p.get('entry_route')=='STRATEGY_EVENT' and not playbook['accepted']:
            q={**q,'accepted':False,'reason':playbook['reason']}
            assessments[p['id']]=q
        if q['accepted'] and not bzu_policy.is_new(p) and playbook_mode=='STRICT' and not playbook['accepted']:
            q={**q,'accepted':False,'reason':playbook['reason']}
            assessments[p['id']]=q
        if q['accepted'] and not bzu_policy.is_new(p) and not meta_decision['accepted']:
            q={**q,'accepted':False,'reason':meta_decision['reason']}
            assessments[p['id']]=q
        if bzu_policy.is_new(p):
            points=quality.confluence(p,s,proof,cfg,sys.modules[__name__])
            p['confluence']=points
            p['score']=points['score']
            p['rating']=points['rating']
            if p.get('policy')==bzu_policy.POLICY:
                p['session_vwap']=policy.vwap_priority(p,s,cfg.tick)
                if points['accepted'] and p['session_vwap']['aligned']:
                    p['rating']='A+'
            next(c for c in p['entry_checks'] if c['key']=='CONTEXT_SCORE')['met']=points['accepted']
            if q['accepted'] and not points['accepted']:
                q={**q,'accepted':False,'reason':points['reason']}
            assessments[p['id']]=q
        p['quality'] = q
        for path in admission_paths:
            if path['event_key']==p['event_key']:
                path.update(reason=q['reason'] if not q['accepted'] else 'ACCEPTED',
                            score=p['score'], rating=p.get('rating'), entry=p['entry'], entry_source='PLANNER',
                            **{k:p[k] for k in ('stop','risk','tp1','tp2','readiness_pct') if k in p})
        checks=p.setdefault('entry_checks',scanner.entry_checks())
        # Accepted planner proposals have already passed the first six gates.
        # This also keeps injected replay planners under the same contract.
        for check in checks:
            if check['key'] not in ('STRUCTURAL_CONFIRMATION','DECISION_EVALUATED') and check['met'] is None:
                check['met']=True
            if check['key']=='STRUCTURAL_CONFIRMATION':
                check['met']=quality.structural_reason(q['features'])=='CONFIRMED'
            if check['key']=='DECISION_EVALUATED':
                check['met']=True
        p['probability'] = meta_decision['probability']
        p['decision_score'] = p['score']+q['ranking_adjustment']
        p['direction_score'] = policy.adjusted_score(p,proof)+meta_decision.get('score_adjustment',0)
        if playbook['accepted']:
            # A capped structural tie-break, NOT a calibrated probability.
            # Family identity persists for downstream attribution/research.
            p['strategy_family'] = playbook['strategy']
            p['strategy_regime'] = playbook['regime']
            p['strategy_evidence_score'] = playbook['evidence_score']
            p['direction_score'] += min(3.,max(0.,(playbook['evidence_score']-60.)*.2))
        if bzu_policy.is_new(p):
            p['direction_score']=p['score']+p.get('session_vwap',{}).get('priority_points',0)
        p['decision_score'] = p['direction_score']
        p['probability_status'] = meta_decision['status']
        p['forecast'] = 'Confirmed departure; wait for return to limit, preserve structural stop, seek nearest target'
        row=next((r for r in matrix if r.get('signal_id')==p['id']),None)
        if row is not None:
            row.update(reason=q['reason'],status='LIMIT_READY' if q['accepted'] else 'BLOCKED',
                       statistics=q['statistics'])
            row['entry_checks']=copy.deepcopy(checks)
            scanner.update_readiness(row)
        p['readiness_pct']=100. if q['accepted'] else round(100*sum(c['met'] is True for c in checks)/len(checks),1)
        p['ict_admission_reason']=ict_reason
        p['distance_pct']=scanner.proximity(s.price,p['entry'])['distance_pct']
    for row in matrix:
        alternatives=[p for p in plans if p['setup']==row['setup'] and scanner.timeframe(p)==row['timeframe']]
        if alternatives:
            best=max(alternatives,key=lambda p:(p['quality']['accepted'],scanner.selection_key(p,s.price)))
            q=best['quality']
            scanner.replace_plan_diagnostics(row, {k:best[k] for k in scanner.PLAN_DIAGNOSTICS if k in best})
            row['score_status']='CONTEXT'
            row.update(signal_id=best['id'],event_key=best['event_key'],side=best['side'],level=best['entry'],
                       reason=q['reason'],status='LIMIT_READY' if q['accepted'] else 'BLOCKED',
                       score=best['score'],entry_checks=copy.deepcopy(best['entry_checks']),statistics=q['statistics'])
            if best.get('setup_conditions'):
                row['setup_conditions']=copy.deepcopy(best['setup_conditions'])
            scanner.update_readiness(row)
    def priority(p):
        q=p['quality'];stat=q['statistics']
        structural=quality.structural_reason(q['features'])=='CONFIRMED'
        return (q['accepted'],*scanner.selection_key(p,s.price))
    plans.sort(key=priority,reverse=True)
    eligible_clusters=[p for p in plans if p['quality']['accepted']]
    clusters=bzu_policy.merge_plans(eligible_clusters,s,cfg,sys.modules[__name__])
    clusters += [p for p in plans if not p['quality']['accepted']]
    for p in clusters:
        p.setdefault('supporting_setups',[p['setup']])
        p.setdefault('supporting_families',[p['canonical_setup_family']])
    # Correlated votes do not manufacture a higher success probability or score.
    clusters.sort(key=priority,reverse=True)
    # Freeze the same target-selection policy for shadow labels and actual
    # decisions. Bound computation; unmodeled alternatives do not become labels.
    modeled=clusters[:6]
    for plan in modeled:
        lifecycle_forecast.select_full_exit(plan,s,cfg,sys.modules[__name__])
    quality.watch_candidates(state,modeled,assessments,cfg)
    ledger['unobserved_due_policy_budget']=ledger.get('unobserved_due_policy_budget',0)+max(0,len(clusters)-len(modeled))
    accepted = []
    for p in clusters:
        q = p['quality']
        if q['accepted']:
            accepted.append(p)
        else:
            refused[q['reason']] = refused.get(q['reason'],0)+1
            watch.append({'setup':p['setup'],'side':p['side'],'level':p['entry'],'reason':q['reason'],'score':p['score']})
    contexts = {tf:{**bias(s.candles[tf]), "side": core.side_sign(tf_contexts[tf]['structure15']["direction"])}
                for tf in ("15m","1H","4H")}
    # Show useful forming setups even when NO detector has produced a plan.
    # Keep all 72 evaluations in state for the dashboard and diagnostics.
    for row in matrix:
        if row.get('side') and 'statistics' not in row:
            family,kind=quality.CONTRACTS[row['setup']]
            native=tf_contexts[row['timeframe']]
            side=row['side'];agree=sum(contexts[t]['side']==side for t in ('1H','4H'))
            opposed=sum(contexts[t]['side']==-side for t in ('1H','4H'))
            alignment='mixed' if agree and opposed else 'aligned' if agree else 'against' if opposed else 'neutral'
            query={'setup':row['setup'],'family':family,'kind':kind,'side':side,
                   'regime':native.get('regime','UNKNOWN'),'alignment':alignment,
                   'setup_timeframe':row['timeframe']}
            row['statistics']=quality.empirical(query,ledger['observations'],s.now)
    actionable=[r for r in matrix if r.get('level') and r.get('readiness_pct',0) is not None
                and r.get('setup_readiness_pct',0)>=50 and r.get('reason') not in
                ('LEVEL_OR_EVENT_COOLDOWN','ANCHOR_INVALIDATED','LEVEL_ALREADY_VISITED_AFTER_OBSERVATION',
                 'ANCHOR_EXPIRED_AWAITING_NEW_EVENT')]
    actionable.sort(key=lambda r:scanner.selection_key(r,s.price),reverse=True)
    direct_waiting=[p for p in watch if p.get("reason")=="WAIT_RETEST"]
    watch=actionable[:24]+direct_waiting[:4]
    directional={}
    for label,side in (('LONG',1),('SHORT',-1)):
        subset=[path for path in admission_paths if path['side']==side]
        reasons={}
        for path in subset:
            if path['reason']!='ACCEPTED':reasons[path['reason']]=reasons.get(path['reason'],0)+1
        directional[label]={'evaluated_paths':len(subset),
            'accepted_paths':sum(path['reason']=='ACCEPTED' for path in subset),'rejections':reasons}
    direct_paths=[p for p in admission_paths if p.get('route')=='STRATEGY_EVENT']
    direct_report={'recovered_scan_gap':sum(x.recovered_from_scan_gap for x in organic_setups),
                   'observed':len(organic_setups),'regime_qualified':len(direct_anchors),
                   'geometry_qualified':sum(1 for p in plans if p.get('entry_route')=='STRATEGY_EVENT'),
                   'admitted':sum(1 for p in accepted if p.get('entry_route')=='STRATEGY_EVENT'),
                   'rejections':dict(__import__('collections').Counter(
                       p['reason'] for p in direct_paths if p['reason']!='ACCEPTED'))}
    return {"health": [],"plans": accepted,"direct_strategy":direct_report,"admission_by_side":directional,"candidate_plans":clusters,"quality_assessments":assessments,
            "meta_model":ledger['last_meta_diagnostic'],
            "watch":watch,"setup_matrix":matrix,"poi_paths":admission_paths,"timeframe_contexts":tf_contexts,"early_context":early_context,
            "rejections": refused,"contexts": contexts,"full_context": ctx,
            "detectors": detector_audit,"setup_statistics": stats,
            "raw_plans": len(plans),"registered_setups": len(core.DETECTORS),
            "timeframe_evaluations":len(matrix),
            "funnel":{"evaluated":len(matrix),"detected":len(fresh),"live_anchors":len(anchors),
                      "geometry_passed":len(plans),"quality_passed":len(accepted)}}


def core_setup_registry(core):
    # Detector names are not derived from enum names (DIRECTION_FLIP differs).
    from setup_scanner import SETUP_NAMES
    if set(SETUP_NAMES)!=set(core.DETECTED_SETUP_TYPES):
        raise ValueError('Scanner conditions must cover all 24 original setups')
    return [{'setup':name} for name in SETUP_NAMES]






def new_state(cfg: Config) -> dict:
    return {"schema": SCHEMA, "version": VERSION, "instrument": cfg.instrument,
            "config_hash": ident(json.dumps(config_dict(cfg), sort_keys=True)),"runtime_config":config_dict(cfg),
            "pending": None, "active": None, "used_events": {}, "trades": [],
            "orders": [], "events": [], "signals": [], "last_run_ts": 0,
            "notification_queue": [], "reconciliation": None,
            "anchor_memory": {}, "level_cooldown": {}, "setup_statistics": {},
            "ledger_revision": 2, "order_sequence": 0,
            "second_slot": {"pending": None, "active": None, "reconciliation": None},
            "pending_orders": [], "pending_policy":PENDING_POLICY if oil_risk.enabled(cfg) else LEGACY_PENDING_POLICY,
            "portfolio_policy":PENDING_POLICY if oil_risk.enabled(cfg) else LEGACY_PENDING_POLICY}


def slot_views(state: dict):
    """Two independent lifecycles share one event/history ledger.

    The original pending/active fields remain slot 1 to preserve v12 state
    identity and compatibility. A shallow view shares lists, not open plans.
    """
    yield 1, state
    extra = state.get('second_slot', {'pending':None, 'active':None, 'reconciliation':None})
    yield 2, {**state, **extra}


def store_slot(state: dict, number: int, view: dict) -> None:
    if number == 2:
        state['second_slot'] = {key:view.get(key) for key in ('pending','active','reconciliation')}


def pending_plans(state: dict) -> list[dict]:
    return [view['pending'] for _,view in slot_views(state) if view.get('pending')]+state.get('pending_orders',[])


def active_plans(state: dict) -> list[dict]:
    return [view['active'] for _,view in slot_views(state) if view.get('active')]


def open_plans(state: dict) -> list[dict]:
    return active_plans(state)+pending_plans(state)


def occupied_slots(state: dict) -> int:
    # Only a filled position or an unresolved possible fill consumes capacity.
    return sum(bool(view.get('active') or view.get('reconciliation')) for _,view in slot_views(state))


def park_pending(state: dict, waiting: list[dict]) -> None:
    """Compatibility fields hold two slots; legacy waiting overflow is replayed/cancelled."""
    state['pending']=None
    state.setdefault('second_slot',{})['pending']=None
    remaining=list(waiting)
    for plan in remaining:
        plan.pop('slot',None)  # Waiting plans never own a position slot.
    for number,view in slot_views(state):
        if not view.get('active') and not view.get('reconciliation') and remaining:
            view['pending']=remaining.pop(0)
            store_slot(state,number,view)
    state['pending_orders']=remaining


def entry_priority(state: dict, now: int, price: float, cfg: Config, step: int = 180_000) -> list[dict]:
    """Decide BEFORE observing candle extremes; never pick fills in hindsight."""
    active=active_plans(state)
    # The old dependency test deliberately ignored opposite sides. That allowed
    # a 15m LONG and 4H SHORT to fill in the same three-minute candle.
    active_sides={p['side'] for p in active}
    if len(active_sides)>1:
        return []  # Legacy hedge: do not add risk; manage both to their exits.
    locked_side=next(iter(active_sides),0)
    # A filled position owns the directional thesis: never fill another limit
    # before the next 15m review, even if it shares the same direction.
    new_book=all(bzu_policy.is_new(p) for p in open_plans(state))
    single = state.get('pending_policy') == PENDING_POLICY
    capacity=0 if active else (max(0, (1 if single else 2)-occupied_slots(state)) if new_book else 1)
    def entry_key(p):
        return execution_atlas.entry_rank(p,price,scanner,cfg.tick)
    ranked=sorted(pending_plans(state),key=entry_key,reverse=True)
    eligible=[]
    for plan in ranked:
        if len(eligible)>=capacity:
            break
        if bzu_policy.is_precision(plan):
            if not bzu_policy.session_gate(now)['allowed']:
                continue
            if not arbitration.loss_cooldown(plan,state['trades'],now)['allowed']:
                continue
        effective=plan.get('effective_ts',plan['placed_ts'])
        partial_legacy='effective_ts' not in plan and now<effective<now+step
        cancel=plan.get('cancel_effective_ts',plan.get('cancel_requested_ts',0))
        if (effective>now and not partial_legacy) or plan['expires_ts']<=now or (cancel and cancel<=now):
            continue
        if (plan.get('queue_execution_policy')=='PASSIVE_ARM_V2'
                and now>=plan.get('queue_policy_effective_ts',effective)):
            continuous=plan.get('entry_armed_bar')==now-step
            if not continuous and not execution_atlas.passive_at_open(plan,price,cfg.tick):
                continue
        if locked_side and plan['side']!=locked_side:
            continue
        if eligible and plan['side']!=eligible[0]['side']:
            continue  # Never fill two opposing directions in a single bar.
        if any(trade_dependencies.related(p,plan,cfg.tick) for p in active+eligible):
            continue
        eligible.append(plan)
    return eligible


def bind_order_numbers(state: dict) -> None:
    """Permanent readable IDs; historical and current copies share a number."""
    rows = [row for row in state.get('orders',[])+state.get('trades',[])+open_plans(state) if row.get('id')]
    known = {str(row['id']):int(row['order_number']) for row in rows if row.get('order_number')}
    sequence = max([state.get('order_sequence',0)]+list(known.values()))
    for row in rows:
        key = str(row['id'])
        if key not in known:
            sequence += 1
            known[key] = sequence
        row['order_number'] = known[key]
        row['display_id'] = f"#{known[key]:03d}"
    state['order_sequence'] = sequence
    for number,view in slot_views(state):
        if view.get('active'):
            view['active']['slot']=number


PAPER_UNCERTAINTY = frozenset({'PLACEMENT_INSIDE_TOUCHED_BAR', 'EXPIRY_INSIDE_BAR', 'CANCEL_INSIDE_TOUCHED_BAR'})


def release_unconfirmed_paper(state: dict, now: int, cfg: Config) -> None:
    """Keep uncertain outcomes out of WR, release only after their age limit.

    No exchange fill is acknowledged. An unknown live/manual reconciliation
    stays blocked. Historical PAPER intervals stay in the ledger for audit.
    """
    for number,view in slot_views(state):
        r = view.get('reconciliation')
        if not r or r.get('reason') not in PAPER_UNCERTAINTY:
            continue
        order = next((o for o in reversed(state['orders']) if o['id']==r['order_id']),None)
        if not order or order.get('execution') != 'PAPER_SIGNAL':
            continue
        if bzu_policy.is_new(order):
            continue  # No automatic time exit can prove this possible position closed.
        if order.get('tp1_decision_gate'):
            continue  # No finite automatic holding exit to justify erasing a possible fill.
        # Unknown fill can occur anywhere before expiry. Retain its slot until
        # even the latest possible fill has reached the intraday age boundary.
        expiry = order.get('expires_ts',r['ts'])
        release_ts = expiry+order.get('max_hold_minutes',cfg.max_hold_minutes)*60_000
        if order.get('intraday',True):
            release_ts=min(release_ts,session_cutoff(expiry,cfg))
        release_ts=max(r['ts'],release_ts)
        if now < release_ts:
            r['release_ts'] = release_ts
            continue
        order['original_status'] = order.get('original_status',order['status'])
        order['status'] = 'UNCONFIRMED'
        order['excluded_from_win_rate'] = True
        order['released_ts'] = now
        state.setdefault('reconciliation_history',[]).append({**copy.deepcopy(r),'released_ts':now})
        emit(state,'UNCONFIRMED_RELEASED',now,order_id=order['id'],reason=r['reason'])
        view['reconciliation'] = None
        store_slot(state,number,view)


def emit(state: dict, kind: str, ts: int, **fields: Any) -> None:
    state["events"].append({"id": ident(kind, ts, fields.get("order_id", ""), len(state["events"])),
                            "kind": kind, "ts": ts, "time": iso(ts), **fields})


def execution_series(s):
    return ('1m',60_000) if s.candles.get('1m') else ('3m',180_000)


def execution_health(s: Snapshot, state: dict) -> list[str]:
    tf,step=execution_series(s)
    rows=s.candles.get(tf,[])
    if s.metadata.get('require_1m') and tf!='1m':
        return ['MISSING_1M_FOR_LIFECYCLE']
    if not rows:
        return ['NO_3M_FOR_LIFECYCLE']
    checkpoint=state.get('last_run_ts',0)
    if checkpoint:
        first=(checkpoint//step)*step
        needed=[c for c in rows if c.ts>=first]
        if first+step<=s.now and (not needed or needed[0].ts!=first):
            return ['LIFECYCLE_HISTORY_GAP']
        if any(b.ts-a.ts!=step for a,b in zip(needed,needed[1:])):
            return ['LIFECYCLE_HISTORY_GAP']
        if needed and (s.now//step)*step>needed[-1].ts+step:
            return ['LIFECYCLE_HISTORY_GAP']
    return []


def end_order(state: dict, order: dict, status: str, reason: str, ts: int) -> None:
    order.update(status=status, reason=reason, resolved_ts=ts)
    state["orders"].append(copy.deepcopy(order))
    state["pending"] = None
    if status == "UNRESOLVED":
        state["reconciliation"] = {"order_id": order["id"], "reason": reason, "ts": ts}
    state["used_events"][order["event_key"]] = ts
    for key in order.get("parent_keys", []):
        state["used_events"][key] = ts
    if order.get("level_key"):
        state.setdefault("level_cooldown", {})[order["level_key"]] = ts+90*60_000
    emit(state, status, ts, order_id=order["id"], reason=reason)


def close_trade(state: dict, p: dict, price: float, ts: int, reason: str, cfg: Config,
                ambiguous: bool = False) -> None:
    if bzu_policy.is_new(p):
        return bzu_policy.close(state,p,price,ts,reason,cfg,sys.modules[__name__],ambiguous)
    cfg = execution_config(p,cfg)
    side, risk = p["side"], p["risk"]
    remaining = p.get("remaining",1.0)
    gross = p.get("realized_gross_r",0)+remaining*side*(price-p["entry"])/risk
    fees = (p["entry"]*p.get("entry_fee_rate",cfg.maker_fee)/risk+p.get("realized_exit_fees_r",0)+remaining*price*cfg.taker_fee/risk) if cfg.include_commission else 0.
    closed = {**copy.deepcopy(p), "status": "CLOSED", "exit": price, "closed_ts": ts,
              "remaining": 0.0, "final_exit_fraction": remaining,
              "closed_at": iso(ts), "close_reason": reason, "gross_r": gross,
              "fees_r": fees, "net_r": gross-fees, "pnl_r": gross-fees,
              "stop_initial": p["initial_stop"], "stop_at_close": p["stop"],
              "bot_version_at_entry": trade_entry_version(p),
              "architecture_version_at_entry": SCHEMA,
              "result": "WIN" if gross-fees>0 else "LOSS" if gross-fees<0 else "BREAKEVEN",
              "close_action": reason, "ambiguous_ohlc": ambiguous,
              "version": trade_entry_version(p), "exit_version": VERSION, "execution": "PAPER_SIGNAL"}
    if not any(t["id"] == closed["id"] for t in state["trades"]):
        state["trades"].append(closed)
    for record in state['orders']:
        if record['id']==p['id']:
            record.update(position_status='CLOSED',closed_ts=ts,trade_net_r=closed['net_r'],
                          trade_result=closed['result'])
    state["active"] = None
    emit(state, "CLOSED", ts, order_id=p["id"], reason=reason, net_r=closed["net_r"],
         ambiguous_ohlc=ambiguous)


def use_full_exit(plan: dict, ts: int) -> bool:
    """Keep all targets; change future sizing without restoring realized volume."""
    if bzu_policy.is_new(plan):
        return False
    if plan.get('exit_policy') == 'SINGLE_FULL_EXIT':
        return False
    active = plan.get('status') == 'OPEN' or 'filled_ts' in plan
    names = [name for name in ('TP1','TP2','TP3')
             if not (active and plan.get(name.lower()+'_hit'))
             and plan.get('partials', {'TP1':1.}).get(name,0)>0]
    name = names[0] if names else 'TP1'
    target = plan.get(name.lower(), plan['tp'])
    plan['legacy_exit_terms'] = {key: copy.deepcopy(plan[key]) for key in
        ('tp','tp1','tp2','tp3','partials','tp1_hit','tp2_hit','tp3_hit') if key in plan}
    plan.update(exit_policy='SINGLE_FULL_EXIT', exit_policy_changed_ts=ts,
                tp=target, full_exit_target=name, partials={name:1.},target_management='ADAPTIVE_FULL_VOLUME',tp1_decision_gate=True,
                continuation_decided=bool(active and plan.get('tp1_hit')))
    plan.setdefault('tp1',target)
    # Older levels remain visible. Without a fresh path audit only the nearest
    # unpassed old target is eligible for a full exit, never a speculative runner.
    plan['full_exit_candidates']=[{'name':name,'price':target}]
    if not plan.get('tp1_hit') and plan.get('pending_stop'):
        plan['legacy_pending_stop']=plan.pop('pending_stop')
    for key in ('decision_forecast','outcome_forecast'):
        if plan.get(key):
            plan['legacy_'+key] = plan.pop(key)
    return True


def upgrade_open_exits(state: dict, ts: int) -> None:
    for plan in open_plans(state):
        if use_full_exit(plan,ts):
            state.setdefault('exit_policy_migrations',[]).append({
                'order_id':plan['id'], 'ts':ts, 'to':'SINGLE_FULL_EXIT',
                'target':plan['tp'], 'remaining':plan.get('remaining',1.)})


def _legacy_partial_exit(state: dict, p: dict, name: str, level: float, ts: int, cfg: Config) -> None:
    # Only replay of intervals BEFORE the one-time policy change uses this.
    cfg = execution_config(p,cfg)
    size = min(p.get("remaining",1.0),p.get("partials",{}).get(name,1.0 if name=="TP1" else 0))
    p[name.lower()+"_hit"] = True
    if size <= 0:
        return
    exit_price = level*(1-p["side"]*cfg.slippage_bps/10_000)
    p["realized_gross_r"] = p.get("realized_gross_r",0)+size*p["side"]*(exit_price-p["entry"])/p["risk"]
    p["realized_exit_fees_r"] = p.get("realized_exit_fees_r",0)+size*exit_price*fee_rate(cfg,"taker")/p["risk"]
    p["remaining"] = max(0,p.get("remaining",1.0)-size)
    p.setdefault("realized_legs",[]).append({"target": name,"price": exit_price,"fraction": size,"ts": ts})
    emit(state,name,ts,order_id=p["id"],price=exit_price,fraction=size)


def manage_bar(state: dict, c: Candle, cfg: Config, fill_bar: bool = False, step: int = 180_000, snapshot=None) -> None:
    p = state["active"]
    if not p:
        return
    if bzu_policy.is_new(p):
        return bzu_policy.manage(state,c,cfg,sys.modules[__name__],step,fill_bar,snapshot)
    cfg = execution_config(p,cfg)
    side, entry, risk = p["side"], p["entry"], p["risk"]
    ts = c.ts+step
    if c.ts <= p.get("last_bar", -1):
        return
    target_amend=p.get('pending_target')
    if target_amend and c.ts>=target_amend['effective_ts']:
        p.update(tp=target_amend['price'],full_exit_target=target_amend['name'],
                 partials={target_amend['name']:1.})
        p.pop('pending_target')
    bzu_policy.apply_protective_stop(p, c.ts)
    stop_hit = c.low <= p["stop"] if side == 1 else c.high >= p["stop"]
    full_exit = p.get('exit_policy') == 'SINGLE_FULL_EXIT'
    targets = [('TP',p['tp'])] if full_exit else [
        (name,p.get(name.lower(),p['tp'])) for name in ('TP1','TP2','TP3')
        if not p.get(name.lower()+'_hit') and p.get('partials',{'TP1':1.}).get(name,0)>0]
    tp_hit = any(c.high >= level if side == 1 else c.low <= level for _,level in targets)
    open_stop = side*(c.open-p["stop"]) <= 0
    # Whole-bar excursions cannot establish post-fill MFE in the entry bar.
    if not fill_bar:
        favorable = c.high if side == 1 else c.low
        adverse = c.low if side == 1 else c.high
        p["mfe_r"] = max(p.get("mfe_r", 0), side*(favorable-entry)/risk)
        p["mae_r"] = max(p.get("mae_r", 0), side*(entry-adverse)/risk)
    if stop_hit:
        raw_exit = c.open if open_stop else p["stop"]
        exit_price = raw_exit*(1-side*cfg.slippage_bps/10_000)
        close_trade(state, p, exit_price, ts, "STOP_GAP" if open_stop else "STOP", cfg,
                    ambiguous=tp_hit or fill_bar)
        return
    # On the fill candle the favorable extreme may have happened BEFORE entry.
    # A close beyond TP proves price traversed TP AFTER entering; otherwise defer.
    before_tp1=not p.get('tp1_hit')
    if full_exit:
        for milestone in ('TP1','TP2','TP3'):
            level=p.get(milestone.lower())
            if level is not None and not p.get(milestone.lower()+'_hit'):
                reached=c.high>=level if side==1 else c.low<=level
                if reached and (not fill_bar or side*(c.close-level)>=0):
                    p[milestone.lower()+'_hit']=True
    if p.get('tp1_decision_gate') and before_tp1 and p.get('tp1_hit'):
        p['awaiting_tp1_review']=True
        p['tp1_reached_ts']=ts
        emit(state,'TP1_REACHED',ts,order_id=p['id'],price=p.get('tp1',p['tp']))
    for name,level in targets:
        reached = c.high >= level if side==1 else c.low <= level
        if reached and (not fill_bar or side*(c.close-level)>=0):
            if full_exit:
                if p.get('tp1_decision_gate') and (p.get('awaiting_tp1_review') or not p.get('continuation_decided')):
                    continue  # First TP is a decision gate, not an automatic partial/full exit.
                p[p.get('full_exit_target','TP1').lower()+'_hit'] = True
                close_trade(state,p,level*(1-side*cfg.slippage_bps/10_000),ts,'TAKE_PROFIT',cfg)
                return
            _legacy_partial_exit(state,p,name,level,ts,cfg)
            if p.get("remaining",1.0) <= 1e-8:
                close_trade(state,p,level*(1-side*cfg.slippage_bps/10_000),ts,"TAKE_PROFIT",cfg)
                return
    if not full_exit and p.get("tp0") and not p.get("tp0_hit"):
        if (c.high>=p["tp0"] if side==1 else c.low<=p["tp0"]) and (not fill_bar or side*(c.close-p["tp0"])>=0):
            p["tp0_hit"] = True
            emit(state,"TP0_MARKER",ts,order_id=p["id"],price=p["tp0"])
    if ts >= p["exit_deadline"] and not p.get("tp1_decision_gate"):
        close_trade(state, p, c.close*(1-side*cfg.slippage_bps/10_000), ts, "INTRADAY_TIME_EXIT", cfg)
        return
    if not fill_bar:
        close_r = side*(c.close-entry)/risk
        p["strong_closes"] = p.get("strong_closes", 0)+1 if close_r >= cfg.break_even_trigger_r else 0
    bzu_policy.arm_breakeven(state, p, snapshot, ts, c.close, cfg, sys.modules[__name__])
    p["last_bar"] = c.ts


def review_full_exit(state: dict, s: Snapshot, cfg: Config) -> None:
    """Current-information decision, effective forward; never a retroactive TP."""
    p=state.get('active')
    if not p or p.get('target_management')!='ADAPTIVE_FULL_VOLUME' or p.get('pending_target'):
        return
    rows=limit_structure.closed(s,'3m')
    if not p.get('tp1_hit'):
        # New entries only: the original configured risk horizon is a review
        # deadline, not unlimited permission to ride a confirmed losing thesis.
        # Legacy active positions retain their exact historical exit contract.
        if trade_entry_version(p)==VERSION:
            failure=entry_adverse_selection.failed_thesis_after_deadline(p,s,cfg.tick)
            if failure:
                p.setdefault('risk_reviews',[]).append(failure)
                actual=execution_config(p,cfg)
                close_trade(state,p,s.price*(1-p['side']*actual.slippage_bps/10_000),
                            s.now,'POST_DEADLINE_THESIS_INVALIDATED',cfg)
        return  # No ordinary discretionary exit/extension before TP1.
    if len(rows)<2 or p.get('last_target_review_ts',0)>=s.now:
        return
    p['last_target_review_ts']=s.now
    side=p['side'];risk=p['risk'];first=p.get('tp1',p['tp'])
    # Reassess near the first destination, rather than chase distant targets
    # immediately after entry. This does not change any target geometry.
    recent=rows[-2:]
    momentum=(side*(recent[-1].close-recent[-2].close)>0
              and all(side*(c.close-c.open)>0 for c in recent))
    opposing=sum(bias(limit_structure.closed(s,tf))['side']==-side for tf in ('1H','4H'))==2
    barriers=p.get('exit_path_barriers',[])
    fresh=cfg.core is not None and all(len(limit_structure.closed(s,tf))>=MIN_BARS[tf] for tf in ('15m','1H','4H'))
    if fresh:
        context={'analysis_timeframe':scanner.timeframe(p),'session':{}}
        barriers=[t for t in full_targets(context,s,'LONG' if side==1 else 'SHORT',p['entry'],cfg)
                  if t.get('hard_barrier',True)]
    milestones=[(name,p.get(name.lower())) for name in ('TP1','TP2','TP3')]
    reference=first
    for _,level in milestones:
        if level is not None and side*(s.price-level)>=-.25*risk and side*(level-reference)>0:
            reference=level
    higher=[(name,level) for name,level in milestones if level is not None
            and side*(level-s.price)>cfg.tick and side*(level-reference)>cfg.tick]
    buffer=max(2*cfg.tick,.15*atr(rows[-20:]))
    eligible_names={o['name'] for o in p.get('full_exit_candidates',[])}
    def clear(name,level):
        return (fresh or name in eligible_names) and not any(
            0<side*(t['level']-s.price)<=side*(level-s.price)+buffer for t in barriers)
    near_first=side*(s.price-first)>=-.25*risk or p.get('tp1_hit')
    blocked_goal=not clear(p.get('full_exit_target','TP1'),p['tp'])
    if not near_first and not (fresh and blocked_goal):
        return
    continuation=next(((name,level) for name,level in higher if clear(name,level)),None)
    if not continuation and side*(p['tp']-s.price)>cfg.tick and clear(p.get('full_exit_target','TP1'),p['tp']):
        continuation=(p.get('full_exit_target','TP1'),p['tp'])
    if momentum and not opposing and continuation:
        name,level=continuation
        # Do not shorten a valid preselected target while momentum is intact.
        current_name=p.get('full_exit_target','TP1')
        if side*(p['tp']-level)>0 and clear(current_name,p['tp']):
            name,level=current_name,p['tp']
        action='CONTINUE_WITH_FULL_VOLUME'
    else:
        name,level='TP1',first
        action='PROTECT_FULL_RESULT'
        if p.get('tp1_hit') or side*(s.price-first)>=0 or (fresh and blocked_goal):
            p['target_reviews']=(p.get('target_reviews',[])+[{'ts':s.now,'action':'CLOSE_FULL_NOW','price':s.price,
                'momentum':momentum,'fresh_zones':fresh,'blocked_goal':blocked_goal}])[-100:]
            actual=execution_config(p,cfg)
            close_trade(state,p,s.price*(1-side*actual.slippage_bps/10_000),s.now,
                        'ADAPTIVE_FULL_EXIT',cfg)
            return
    decision={'ts':s.now,'action':action,'target':name,'price':level,
              'momentum':momentum,'path_clear':bool(continuation),'fresh_zones':fresh}
    p['target_reviews']=(p.get('target_reviews',[])+[decision])[-100:]
    p['awaiting_tp1_review']=False
    p['continuation_decided']=True
    if name==p.get('full_exit_target','TP1') and abs(level-p['tp'])<cfg.tick/2:
        return
    effective=((s.now+TF['3m']-1)//TF['3m'])*TF['3m']
    p['pending_target']={'name':name,'price':level,'effective_ts':effective}
    emit(state,'FULL_TARGET_AMENDED',s.now,order_id=p['id'],target=name,price=level,effective_ts=effective)


def _amend_slot_at_report(state: dict, s: Snapshot, cfg: Config) -> None:
    active = state.get('active')
    if active and not bzu_policy.is_new(active):
        review_full_exit(state, s, cfg)


def amend_at_report(state: dict, s: Snapshot, cfg: Config) -> None:
    for number,view in slot_views(state):
        _amend_slot_at_report(view,s,cfg)
        store_slot(state,number,view)


def _advance_slot_lifecycle(state: dict, s: Snapshot, cfg: Config, allow_fill: bool = True) -> None:
    """Chronological replay before applying CURRENT context. No retroactive cancel."""
    tf, step = execution_series(s)
    for c in s.candles.get(tf, []):
        if not c.confirmed or c.ts+step>s.now:
            continue
        if state["active"]:
            manage_bar(state, c, cfg, step=step, snapshot=s)
            continue
        p = state["pending"]
        if not p or c.ts <= p.get("last_bar", -1):
            continue
        end = c.ts+step
        effective = p.get('effective_ts',p['placed_ts'])
        if c.ts < effective:
            if bzu_policy.is_new(p) and step==60_000 and c.ts<p['placed_ts']<end:
                touched=c.low<=p['entry'] if p['side']==1 else c.high>=p['entry']
                if touched:
                    end_order(state,p,'UNRESOLVED','PLACEMENT_INSIDE_TOUCHED_BAR',end)
                    continue
            if 'effective_ts' not in p and end > p["placed_ts"]:
                possible_fill = allow_fill and (c.low < p["entry"]-cfg.tick/2 if p["side"] == 1 else c.high > p["entry"]+cfg.tick/2)
                if possible_fill:
                    end_order(state, p, "UNRESOLVED", "PLACEMENT_INSIDE_TOUCHED_BAR", end)
            continue
        cancel_ts = p.get("cancel_effective_ts",p.get("cancel_requested_ts", 0))
        if cancel_ts and c.ts >= cancel_ts:
            end_order(state, p, "CANCELLED", p["cancel_reason"], cancel_ts)
            continue
        if bzu_policy.is_precision(p):
            # These windows are known BEFORE the candle opens. Cancel without
            # consulting this candle's future high/low, even after a scan gap.
            session=bzu_policy.session_gate(c.ts)
            cooldown=arbitration.loss_cooldown(p,state['trades'],c.ts)
            if not session['allowed'] or not cooldown['allowed']:
                end_order(state,p,'CANCELLED',session['reason'] if not session['allowed']
                          else cooldown['reason'],c.ts)
                continue
        if c.ts >= p["expires_ts"]:
            end_order(state, p, "EXPIRED", p.get('expiry_reason','LIMIT_TTL'), p["expires_ts"])
            continue
        fill = allow_fill and (c.low <= p["entry"]-cfg.tick+cfg.tick*1e-8 if p["side"] == 1 else c.high >= p["entry"]+cfg.tick-cfg.tick*1e-8)
        if bzu_policy.is_new(p):
            fill=allow_fill and (c.low<=p['entry'] if p['side']==1 else c.high>=p['entry'])
        if cancel_ts and c.ts < cancel_ts < end:
            end_order(state, p, "UNRESOLVED" if fill else "CANCELLED",
                      "CANCEL_INSIDE_TOUCHED_BAR" if fill else p["cancel_reason"], cancel_ts)
            continue
        # TTL inside a candle: order of fill vs expiry is unknowable from OHLC.
        if end > p["expires_ts"]:
            end_order(state, p, "UNRESOLVED" if fill else "EXPIRED",
                      "EXPIRY_INSIDE_BAR" if fill else "LIMIT_TTL", p["expires_ts"])
            continue
        if bzu_policy.is_new(p):
            fill = allow_fill and (p.get('action_type')=='ACTIVE_ENTRY' or
                (c.low<=p['entry'] if p['side']==1 else c.high>=p['entry']))
            target = p['tp1']
            target_hit = c.high>=target if p['side']==1 else c.low<=target
            before_fill = p['side']*(c.open-target)>=0
            armed=p.get('target_cancel_armed',True)
            if p['side']*(c.open-target)<0:
                armed=True
            if target_hit and armed and (not fill or before_fill):
                end_order(state,p,'CANCELLED','TARGET_REACHED_WITHOUT_FILL',end)
                continue
            p['target_cancel_armed']=armed or p['side']*(c.close-target)<0
        if fill:
            end_order(state, p, "FILLED", "PAPER_ONE_TICK_PENETRATION", end)
            if bzu_policy.is_new(p):
                p=bzu_policy.first_fill(p,c,sys.modules[__name__],cfg,step)
                state['active']=p
                manage_bar(state,c,cfg,fill_bar=True,step=step)
                continue
            p = copy.deepcopy(p)
            p.update(status="OPEN", filled_ts=c.ts, opened_at=iso(c.ts), last_bar=-1,
                     mfe_r=0.0, mae_r=0.0, strong_closes=0,remaining=1.0,
                     realized_gross_r=0.0,realized_exit_fees_r=0.0,realized_legs=[],
                     entry_version=p.get('entry_version',p.get('version',VERSION)))
            # Fill time is interval-censored. Bar OPEN is a conservative age clock.
            p["fill_time_status"] = "WITHIN_3M_BAR"
            p["exit_deadline"] = c.ts+p.get('max_hold_minutes',cfg.max_hold_minutes)*60_000
            if p.get('intraday',True):
                p['exit_deadline'] = min(p['exit_deadline'], session_cutoff(c.ts, cfg))
            state["active"] = p
            manage_bar(state, c, cfg, fill_bar=True, step=step)
        else:
            p["last_bar"] = c.ts
    p = state.get("pending")
    if p and s.now >= p["expires_ts"]:
        # An unfinished bar crossing expiry is not observable yet. Defer until
        # it closes; guessing an expiry could erase a fill before the deadline.
        expiry_bar = (p["expires_ts"]//step)*step
        if p["expires_ts"] % step == 0 or s.now >= expiry_bar+step:
            end_order(state, p, "EXPIRED", p.get('expiry_reason','LIMIT_TTL'), p["expires_ts"])


def advance_lifecycle(state: dict, s: Snapshot, cfg: Config) -> None:
    tf, step = execution_series(s)
    state['execution_step_ms']=step
    for p in open_plans(state):
        old_step=p.get('last_execution_step_ms',180_000)
        if p.get('last_bar',-1)>=0 and old_step!=step:
            p['last_bar'] += old_step-step
        p['last_execution_step_ms']=step
    if state.get('pending_policy') not in ('UNBOUNDED_QUEUE_ACTIVE_CAP_2','ONE_DIRECTION_TWO_PENDING_MAX',PENDING_POLICY):
        # Catch up the old contract before changing future queue behavior.
        for number,view in slot_views(state):
            _advance_slot_lifecycle(view,s,cfg)
            store_slot(state,number,view)
        return
    followed=open_plans(state)
    cursor=min((max((p['placed_ts']//step*step if bzu_policy.is_new(p) and step==60_000
                    else p.get('effective_ts',p.get('filled_ts',p['placed_ts']//step*step))),p.get('last_bar',0)+step)
                for p in followed),default=s.now)
    rows=[c for c in s.candles.get(tf,[]) if c.confirmed and c.ts+step<=s.now and c.ts>=cursor]
    for c in rows:
        # Freeze priorities using only the bar OPEN and earlier decisions.
        # A close in this bar frees capacity from the NEXT bar, not retroactively.
        eligible={p['id'] for p in entry_priority(state,c.ts,c.open,cfg,step)}
        waiting=pending_plans(state)
        park_pending(state,[])
        for number,view in slot_views(state):
            if view.get('active'):
                manage_bar(view,c,cfg,step=step,snapshot=s)
                store_slot(state,number,view)
        remaining=[]
        for plan in waiting:
            if c.ts<=plan.get('last_bar',-1):
                remaining.append(plan)
                continue
            if bzu_policy.is_precision(plan):
                session=bzu_policy.session_gate(c.ts)
                cooldown=arbitration.loss_cooldown(plan,state['trades'],c.ts)
                if not session['allowed'] or not cooldown['allowed']:
                    book={**state,'pending':plan,'active':None,'reconciliation':None}
                    end_order(book,plan,'CANCELLED',session['reason'] if not session['allowed']
                              else cooldown['reason'],c.ts)
                    continue
            if plan.get('effective_ts',plan['placed_ts'])<=c.ts<plan['expires_ts'] and plan['id'] not in eligible:
                plan['capacity_waited']=True
            if (plan.get('queue_execution_policy')=='PASSIVE_ARM_V2'
                    and c.ts>=plan.get('queue_policy_effective_ts',plan.get('effective_ts',plan['placed_ts']))):
                if plan['id'] in eligible:
                    plan['entry_armed_bar']=c.ts
                else:
                    plan.pop('entry_armed_bar',None)
                    visited=c.low<=plan['entry'] if plan['side']==1 else c.high>=plan['entry']
                    if visited and c.ts<plan['expires_ts'] and not plan.get('cancel_requested_ts'):
                        book={**state,'pending':plan,'active':None,'reconciliation':None}
                        end_order(book,plan,'CANCELLED','LEVEL_PASSED_WHILE_UNARMED',c.ts+step)
                        continue
            book={**state,'pending':plan,'active':None,'reconciliation':None}
            _advance_slot_lifecycle(book,Snapshot(c.ts+step,c.close,{tf:[c]}),cfg,
                                    allow_fill=plan['id'] in eligible)
            if book.get('pending'):
                remaining.append(book['pending'])
            if book.get('active') or book.get('reconciliation'):
                for number,view in slot_views(state):
                    if not view.get('active') and not view.get('reconciliation'):
                        view['active']=book.get('active')
                        view['reconciliation']=book.get('reconciliation')
                        if view.get('active'):
                            view['active']['slot']=number
                        store_slot(state,number,view)
                        break
                else:
                    raise ValueError('Active position cap would be exceeded; refusing a third fill')
        park_pending(state,remaining)
    # Resolve an aligned expiry even when no new candle was needed.
    waiting=[]
    for plan in pending_plans(state):
        book={**state,'pending':plan,'active':None,'reconciliation':None}
        _advance_slot_lifecycle(book,Snapshot(s.now,s.price,{tf:[]}),cfg,allow_fill=False)
        if book.get('pending'):
            waiting.append(book['pending'])
    park_pending(state,waiting)
    reconcile_cancellations_at_closed_boundary(state, s)


def reconcile_cancellations_at_closed_boundary(state: dict, s: Snapshot) -> None:
    """Finalize cancel on its boundary, not a whole candle later.

    The engine has already examined the last possibly executable 3m bar;
    last_bar plus a matching CLOSED bar are required to prove no fill. An
    unknown interval remains reserved and cannot silently become a vacancy.
    """
    tf, step=execution_series(s)
    waiting = []
    for plan in pending_plans(state):
        effective = plan.get('cancel_effective_ts',0)
        if (effective and s.now >= effective and
                plan.get('last_bar',-1) >= effective-step and
                any(c.confirmed and c.ts+step==effective
                    for c in s.candles.get(tf,[]))):
            book={**state,'pending':plan,'active':None,'reconciliation':None}
            end_order(book,plan,'CANCELLED',
                      plan.get('cancel_reason','PORTFOLIO_REPLACEMENT'),effective)
        else:
            waiting.append(plan)
    if len(waiting)!=len(pending_plans(state)):
        park_pending(state,waiting)


def day_stats(state: dict, now: int, cfg: Config) -> dict:
    tz = ZoneInfo(cfg.timezone)
    day = datetime.fromtimestamp(now/1000, tz).date()
    same = lambda ts: datetime.fromtimestamp(ts/1000, tz).date() == day
    trades = [t for t in state["trades"] if t['closed_ts']<=now and same(t["closed_ts"])]
    entries = [o for o in state["orders"] if o["status"] == "FILLED" and o['resolved_ts']<=now and same(o["resolved_ts"])]
    # Descriptive statistics only; no daily entry or loss limit.
    losses = sum(min(0, t["net_r"]) for t in trades)
    return {"net_r": sum(t["net_r"] for t in trades), "losses_r": -losses, "entries": len(entries)}


def request_cancel(state: dict, p: dict, reason: str, now: int) -> None:
    step=state.get('execution_step_ms',180_000)
    if p.get("cancel_requested_ts"):
        return
    before_effective = (reason == 'LEGACY_MICRO_RISK' and p.get('action_type') != 'ACTIVE_ENTRY'
                        and p.get('effective_ts', 0) >= now)
    if now % step == 0 or before_effective:
        if 'pending_orders' in state:
            waiting=[other for other in pending_plans(state) if other['id']!=p['id']]
            book={**state,'pending':p,'active':None,'reconciliation':None}
            end_order(book,p,'CANCELLED',reason,now)
            park_pending(state,waiting)
        else:
            end_order(state,p,'CANCELLED',reason,now)
    else:
        # Current partial candle might contain a fill BEFORE cancellation.
        # Reserve the slot until that candle closes; do not erase that interval.
        p.update(cancel_requested_ts=now, cancel_reason=reason)
        if 'effective_ts' in p:
            p['cancel_effective_ts'] = ((now+step-1)//step)*step
        emit(state, "CANCEL_REQUESTED", now, order_id=p["id"], reason=reason)


def migrate_organic_pending(state, snapshot, cfg):
    """Retire superseded paper limits after lifecycle reconciliation."""
    if not oil_risk.enabled(cfg):
        return
    for order in pending_plans(state):
        if not oil_risk.is_organic(order):
            request_cancel(state, order, 'LEGACY_MICRO_RISK', snapshot.now)


def review_waiting_conditions(state, pending, snapshot, cfg, htf1=0, htf4=0):
    """Shared causal cancellation policy for the book, learning and scenarios."""
    zone = pending.get('entry_zone') or {}
    timeframe = zone.get('timeframe') or pending.get('formation_timeframe')
    if zone.get('kind') == 'MICRO_BOS_FVG' or (timeframe and timeframe not in scanner.PRIMARY):
        request_cancel(state, pending, 'MICRO_ZONE_EXCLUDED', snapshot.now)
        return
    if (pending.get('setup') or pending.get('setup_type')) == 'SESSION_MEAN_RECLAIM':
        request_cancel(state, pending, 'SESSION_MEAN_LIMIT_DISABLED', snapshot.now)
        return
    a15 = atr(policy.closed(snapshot, '15m'))
    if scanner.exhausted_move(pending['side'], snapshot.price, pending.get('tp1') or pending.get('tp')):
        request_cancel(state, pending, 'EXHAUSTED_MOVE', snapshot.now)
        return
    if a15 > 0 and scanner.entry_window(pending['side'], snapshot.price, pending['entry'], a15, cfg,
                                        minimum=False) == 'DISTANT_ZONE':
        request_cancel(state, pending, 'LIMIT_UNREACHABLE', snapshot.now)
        return
    if bzu_policy.is_new(pending):
        if bzu_policy.is_precision(pending):
            session=bzu_policy.session_gate(snapshot.now)
            approach=entry_adverse_selection.approach(snapshot,a15,
                        pending['side'],pending.get('entry_zone'),cfg.tick,stop=pending.get('stop'))
            cooldown=arbitration.loss_cooldown(pending,state['trades'],snapshot.now)
            regime=policy.market_regime(snapshot,cfg.tick,sys.modules[__name__])
            local_ok,local_reason=policy.regime_admission(pending['side'],pending['entry'],regime,
                                   snapshot,cfg.tick,sys.modules[__name__],pending.get('entry_zone'), require_macro=oil_risk.enabled(cfg))
            reason=(session['reason'] if not session['allowed'] else
                    'IMPULSE_CONFIRMATION_REQUIRED' if approach.get('blocked') else
                    cooldown['reason'] if not cooldown['allowed'] else
                    local_reason if not local_ok else None)
            if reason:
                request_cancel(state,pending,reason,snapshot.now)
                return
        if pending['side']*(snapshot.price-pending['tp1'])<0:
            pending['target_cancel_armed']=True
        flip_frames = ('15m', '1H')
        if (bzu_policy.is_precision(pending)
                and regime.get('active_structure', {}).get('side') == pending['side']):
            flip_frames = ('15m',)
        flip=policy.structural_flip(snapshot,-pending['side'],pending['placed_ts'],cfg.tick,
                                    sys.modules[__name__],timeframes=flip_frames)
        if flip['confirmed']:
            request_cancel(state,pending,'STRUCTURE_FLIPPED',snapshot.now)
        elif pending.get('policy') not in (bzu_policy.POLICY, bzu_policy.MOMENTUM_POLICY) and snapshot.now>=session_cutoff(snapshot.now,cfg):
            request_cancel(state,pending,'SESSION_ENDED',snapshot.now)
        elif pending.get('target_cancel_armed',True) and pending['side']*(snapshot.price-pending['tp1'])>=0:
            request_cancel(state,pending,'TARGET_REACHED_WITHOUT_FILL',snapshot.now)
        return
    if cfg.core is not None and setup_paths.recovery_failed(pending,snapshot,cfg,sys.modules[__name__]):
        request_cancel(state,pending,'CAPITULATION_RECOVERY_LOST',snapshot.now)
        return
    side=pending['side'];opposite=-side
    if side*(snapshot.price-pending['tp'])<0:
        pending['target_cancel_armed']=True
    if cfg.core is not None and execution_atlas.control_reversed(pending,snapshot,cfg,sys.modules[__name__]):
        request_cancel(state,pending,'EXECUTION_CONTROL_REVERSED',snapshot.now)
    elif (cfg.cancel_on_bias_flip and htf1==htf4==opposite
          and not (pending.get('htf_1h')==opposite and pending.get('htf_4h')==opposite)):
        request_cancel(state,pending,'CONFIRMED_HTF_FLIP',snapshot.now)
    elif side*(snapshot.price-pending['invalidation'])<=0:
        request_cancel(state,pending,'STRUCTURE_INVALIDATED_NOW',snapshot.now)
    elif pending.get('target_cancel_armed',True) and side*(snapshot.price-pending['tp'])>=0:
        request_cancel(state,pending,'TARGET_REACHED_WITHOUT_FILL',snapshot.now)


def run_cycle(state: dict, s: Snapshot, cfg: Config) -> dict:
    entry_mode = os.environ.get('BZU_ENTRY_MODE', 'PASSIVE_LIMIT').strip().upper()
    if entry_mode not in ('PASSIVE_LIMIT', 'LIVE_REACTION', 'HYBRID'):
        raise ValueError('BZU_ENTRY_MODE must be PASSIVE_LIMIT, LIVE_REACTION or HYBRID')
    if s.now <= state.get("last_run_ts", 0):
        return {"action": "DUPLICATE_OR_OUT_OF_ORDER", "events": [], "analysis": {}}
    if state["instrument"] != cfg.instrument:
        raise ValueError("Instrument changed: use a separate state path")
    config_hash = ident(json.dumps(config_dict(cfg), sort_keys=True))
    if state.get("config_hash") != config_hash and open_plans(state):
        old_config=state.get('runtime_config')
        cadence_only=isinstance(old_config,dict) and {k:v for k,v in old_config.items() if k!='analysis_scan_minutes'}=={
            k:v for k,v in config_dict(cfg).items() if k!='analysis_scan_minutes'}
        score_only = (isinstance(old_config,dict)
                      and state.get('version') == 'full-ict-v12.23.0-master-plan'
                      and old_config.get('min_score') == 65 and cfg.min_score == 60
                      and {**old_config, 'min_score':60} == config_dict(cfg))
        # One-time release migration preserves entry/stop/TTL. Exit policy
        # changes explicitly after catching up the previous observed interval.
        # Later operator configuration changes still require closed slots.
        if state.get('version') not in ('full-ict-v12.2.0-confirmed-ict-quality',
                                       'full-ict-v12.3.0-causal-geometry-reports',
                                       'full-ict-v12.4.0-native-mtf-limit-scanner',
                                       'full-ict-v12.4.1-single-scenario-no-commission',
                                       'full-ict-v12.4.2-quote-gates-price-forecast',
                                       'full-ict-v12.4.3-minimum-sl-tp',
                                       'full-ict-v12.5.0-causal-early-limits',
                                       'full-ict-v12.6.0-lifecycle-adaptive-policy',
                                       'full-ict-v12.7.0-adaptive-full-volume',
                                       'full-ict-v12.8.0-directional-limit-paths',
                                       'full-ict-v12.9.0-anticipatory-execution-atlas',
                                       'full-ict-v12.10.0-causal-direction-control',
                                       'full-ict-v12.11.0-directional-ict-arbiter',
                                       'full-ict-v12.20.0-reviewed-short',
                                       'full-ict-v12.21.0-bzu-checklist') and not (cadence_only or score_only):
            raise ValueError("Configuration changed with an open plan; finish its lifecycle first")
        state.setdefault('config_migrations',[]).append({'from':state.get('version'),
                 'to':VERSION,'ts':s.now,'existing_plans_preserved':True})
    start = len(state["events"])
    faults = health(s, cfg)
    lifecycle_faults = execution_health(s, state) if open_plans(state) else []
    if lifecycle_faults or any(x in faults for x in ("UNTRUSTED_PRICE", "WRONG_INSTRUMENT", "STALE_TICKER", "STALE_3m")):
        return {"action": "DATA_HOLD", "events": [], "analysis": {"health": faults+lifecycle_faults}}
    state["execution_audit"]={"timeframe":execution_series(s)[0],"minute_precision":bool(s.candles.get("1m")),"as_of_ts":s.now}
    release_unconfirmed_paper(state,s.now,cfg)
    bind_order_numbers(state)
    advance_lifecycle(state, s, cfg)
    # First catch up the old entry contract, then change FUTURE queue arming.
    for pending in pending_plans(state):
        if pending.get('queue_execution_policy')!='PASSIVE_ARM_V2':
            pending['queue_execution_policy']='PASSIVE_ARM_V2'
            pending['entry_policy_migrated_ts']=s.now
            pending['queue_policy_effective_ts']=((s.now+TF['3m']-1)//TF['3m'])*TF['3m']
    # Reconcile any historical fill using its original order before migration.
    migrate_organic_pending(state, s, cfg)
    single_limit = oil_risk.enabled(cfg)
    order_limit = 1 if single_limit else 2
    state['pending_policy'] = PENDING_POLICY if single_limit else LEGACY_PENDING_POLICY
    state.setdefault('pending_orders',[])
    amend_at_report(state, s, cfg)
    quality.advance(state,s,cfg,sys.modules[__name__])
    # A non-PAPER reconciliation still needs a human check. Existing positions
    # continue to be managed; one uncertain interval never erases another plan.
    unresolved = [view['reconciliation'] for _,view in slot_views(state) if view.get('reconciliation')]
    def needs_manual_check(r):
        record = next((o for o in reversed(state['orders']) if o['id']==r['order_id']),{})
        return (bzu_policy.is_new(record) or r.get('reason') not in PAPER_UNCERTAINTY
                or record.get('execution')!='PAPER_SIGNAL')
    manual_block = any(needs_manual_check(r) for r in unresolved)
    analysis = analyze(s, cfg, state) if not manual_block else {"health":faults,"plans":[],"watch":[],"rejections":{},"contexts":{}}
    analysis['edge_mode']=os.environ.get('BZU_EDGE_MODE','SHADOW').strip().upper()
    state["setup_statistics"] = analysis.get("setup_statistics",state.get("setup_statistics",{}))
    if analysis.get('full_context'):
        quality.refresh_confirmations(state,s,analysis['full_context'],cfg,sys.modules[__name__],
                                      {**analysis.get('timeframe_contexts',{}),
                                       'MICRO_PRECURSOR':analysis.get('early_context',analysis['full_context'])})
    ctx = analysis.get("contexts", {})
    for pending in list(pending_plans(state)):
        if pending and (ctx or bzu_policy.is_precision(pending)):
            review_waiting_conditions(state,pending,s,cfg,ctx.get('1H',{}).get('side',0),
                                      ctx.get('4H',{}).get('side',0))
    release_unconfirmed_paper(state,s.now,cfg)
    daily = day_stats(state, s.now, cfg)
    busy = open_plans(state)
    action, selected = "WATCH", []
    if any(view.get('reconciliation') for _,view in slot_views(state)):
        action = "RECONCILIATION_REQUIRED"
    elif any(view.get('active') for _,view in slot_views(state)):
        action = "FOLLOW"
    elif busy:
        action = "CANCEL_LIMIT" if any(p.get('cancel_requested_ts') for p in busy) else "WAIT_LIMIT"
    can_place = not manual_block
    session=bzu_policy.session_gate(s.now)
    analysis['session_gate']=session
    if not session['allowed']:
        can_place=False
        if not busy:
            action=session['reason']
    # Do not place a replacement in the very report that first discovers a
    # historical fill (even if it also closed by this report).
    if any(e.get('kind') == 'FILLED' for e in state['events'][start:]):
        can_place = False
    # Independent, final execution interlock: never rely exclusively on the
    # upstream analyzer (or a restored queue/previous model) to enforce the
    # research-only deployment. Existing pending/active orders are preserved.
    if analysis.get('edge_mode') == 'RESEARCH_ONLY':
        can_place = False
        if not busy:
            action = 'RESEARCH_ONLY'
    if can_place and analysis['health']:
        if not busy:
            action = 'DATA_HOLD'
        can_place = False
    # Direction is a portfolio decision, NOT 24 unrelated entry permissions.
    # Existing live positions lock direction until they close. Existing pending
    # orders are canceled causally before any opposite entry can be submitted.
    valid=sorted((p for p in analysis.get('plans',[])
                  if p.get('readiness_pct',100.)>=100.),
                 key=lambda p:policy.ranking(p,s.price,cfg.tick),reverse=True)
    # Compare both directions on the same execution/exit model BEFORE
    # deciding the portfolio direction (not only after selecting one side).
    for candidate in valid[:6]:
        use_full_exit(candidate,s.now)
        lifecycle_forecast.select_full_exit(candidate,s,cfg,sys.modules[__name__])
        adjustment=lifecycle_forecast.decision_adjustment(candidate['outcome_forecast'])
        if bzu_policy.is_new(candidate):
            adjustment=0.
        candidate['direction_score']=round(candidate.get('direction_score',
            candidate.get('decision_score',candidate.get('score',0)))+adjustment,4)
    # The target may have changed since initial analysis; re-evaluate the
    # same frozen model against FINAL executable net RR, without retraining.
    fitted=meta.train(quality.ensure(state,cfg)['observations'],s.now,quality.STRATEGY)
    meta_rejected=0
    surviving=[]
    for candidate in valid:
        prior=candidate.get('meta_decision',{})
        outcome=meta.evaluate(fitted,candidate.get('quality',{}).get('features',{}),
                              net_rr=candidate.get('net_rr',0),costs_r=candidate.get('costs_r',0))
        candidate['meta_decision']=outcome
        candidate['probability']=outcome['probability']
        candidate['probability_status']=outcome['status']
        if bzu_policy.is_new(candidate) or outcome['accepted']:
            surviving.append(candidate)
        else:
            meta_rejected+=1
            analysis.setdefault('rejections',{})[outcome['reason']]=(
                analysis.setdefault('rejections',{}).get(outcome['reason'],0)+1)
            for row in analysis.get('setup_matrix',[]):
                if row.get('signal_id')==candidate.get('id'):
                    row.update(status='BLOCKED',reason=outcome['reason'])
    valid=surviving
    cooled=[]
    for candidate in valid:
        cooldown=arbitration.loss_cooldown(candidate,state['trades'],s.now)
        candidate['loss_cooldown']=cooldown
        if not cooldown['allowed']:
            analysis.setdefault('rejections',{})[cooldown['reason']]=analysis.setdefault('rejections',{}).get(cooldown['reason'],0)+1
            analysis.setdefault('watch',[]).append({'setup':candidate['setup'],'side':candidate['side'],
                 'level':candidate['entry'],'reason':cooldown['reason'],'score':candidate.get('score',0)})
            for row in analysis.get('setup_matrix',[]):
                if row.get('signal_id')==candidate['id']:
                    row.update(status='BLOCKED',reason=cooldown['reason'])
        else:
            cooled.append(candidate)
    valid=cooled
    valid.sort(key=lambda p:policy.ranking(p,s.price,cfg.tick),reverse=True)
    analysis['entry_mode'] = entry_mode
    market = policy.market_regime(s, cfg.tick, sys.modules[__name__])
    regime = market.get('state')
    dominant = policy.regime_side(market)
    confirmed = []
    for candidate in valid:
        zone = {**(candidate.get('entry_zone') or {}), 'side': candidate['side']}
        if (dominant and candidate['side'] != dominant
                and not (candidate.get('zone_origin', {}).get('wave_confirmed')
                         and scanner.wave.reversal_override(candidate['side'], zone, s, cfg.tick, require_macro=single_limit))):
            continue
        if single_limit and not oil_risk.is_organic(candidate):
            continue
        if candidate.get('setup') == 'SESSION_MEAN_RECLAIM' and candidate.get('action_type') != 'ACTIVE_ENTRY':
            continue
        if scanner.candidate_plan_error({**candidate, **zone, 'score_source': 'CONTEXT'},
                                        min_score=60, min_rr=1.40):
            continue
        assessment = scanner.calculate_zone_trade_plan(
            zone, s, cfg, sys.modules[__name__], dominant, analysis.get('full_context', {}),
            geometry=candidate, regime=regime)
        if not assessment or assessment['score'] < 60 or assessment['tactic'] == 'OBSERVE':
            continue
        candidate['zone_quality'] = assessment
        confirmed.append(candidate)
    # Inside-zone and M5 breaker plans require a closed reaction, even in passive mode.
    valid = [p for p in confirmed if p['zone_quality']['entry_condition'] == 'RETEST']
    watch_allowed = can_place
    if entry_mode == 'HYBRID':
        valid = [p for p in valid if p['zone_quality']['tactic'] == 'LIMIT']
    elif entry_mode == 'LIVE_REACTION':
        can_place = False
    active=active_plans(state)
    pending=pending_plans(state)
    # Legacy fills and uncertain executions retain their slots. Cancel excess
    # waiting orders before admitting a single new thesis.
    pending_capacity=max(0,order_limit-occupied_slots(state))
    awaiting=[p for p in pending if not p.get('cancel_requested_ts')]
    if len(awaiting)>pending_capacity:
        ranked_legacy=sorted(awaiting, key=lambda p:policy.ranking(p,s.price,cfg.tick),reverse=True)
        for obsolete in ranked_legacy[pending_capacity:]:
            request_cancel(state,obsolete,'PENDING_SINGLE_LIMIT_CAP' if single_limit else 'LEGACY_PENDING_EXCESS_CAP_2',s.now)
        can_place=False
        pending=pending_plans(state)
    arbitration_audit={'decision':'SKIPPED','reason':'NO_PENDING_OR_UNSAFE_BOOK'}
    rotation=state.get('portfolio_rotation_intent')
    if rotation and (s.now>rotation.get('expires_ts',0) or active):
        state.pop('portfolio_rotation_intent',None)
    if pending and not active and not manual_block and not analysis['health'] and can_place \
            and not any(p.get('cancel_requested_ts') for p in pending):
        proposed_for_rotation,_,_=policy.choose(valid,s.price,cfg.tick,cfg.conflict_margin)
        pending_sides_for_rotation={p['side'] for p in pending}
        if len(pending_sides_for_rotation)==1 and proposed_for_rotation in pending_sides_for_rotation:
            arbitration_audit=arbitration.decision(pending,valid,s,cfg,policy,
                                                   state.get('last_portfolio_swap_ts',0),sys.modules[__name__])
        else:
            arbitration_audit={'decision':'SKIPPED','reason':'DIRECTION_NOT_CONFIRMED_FOR_ROTATION'}
        if arbitration_audit['decision']=='REQUEST_REPLACEMENT':
            obsolete=next(p for p in pending if p['id']==arbitration_audit['replace_id'])
            challenger=next(p for p in valid if p['id']==arbitration_audit['candidate_id'])
            execution_tf,execution_step=execution_series(s)
            at_boundary=(s.now%execution_step==0 and any(
                c.confirmed and c.ts+execution_step==s.now for c in s.candles.get(execution_tf,[])))
            request_cancel(state,obsolete,arbitration_audit['reason'],s.now)
            state['last_portfolio_swap_ts']=s.now
            intent={'candidate_event_key':challenger.get('event_key'),
                    'candidate_id':challenger['id'],'replaced_id':obsolete['id'],
                    'decided_ts':s.now,'expires_ts':min(challenger['expires_ts'],
                                                       s.now+30*60_000),
                    'status':'PREPARED_WAIT_RECONCILIATION'}
            state['portfolio_rotation_intent']=intent
            emit(state,'PORTFOLIO_REPLACEMENT_DECISION',s.now,
                 order_id=obsolete['id'],replacement_id=challenger['id'],
                 advantage=arbitration_audit['advantage'],
                 criterion=arbitration_audit['reason'],
                 fast_path=bool(at_boundary and all(p['id']!=obsolete['id'] for p in pending_plans(state))))
            pending=pending_plans(state)
            if at_boundary and all(p['id']!=obsolete['id'] for p in pending) \
                    and not active_plans(state) and not any(p.get('cancel_requested_ts') for p in pending):
                arbitration_audit['decision']='FAST_ROTATE_SAME_CYCLE'
                intent['status']='READY_TO_ARM'
            else:
                can_place=False
                action='CANCEL_LIMIT'
                arbitration_audit['decision']='WAIT_CANCEL_RECONCILIATION'
    analysis['portfolio_arbitration']=arbitration_audit
    active_sides={p['side'] for p in active}
    pending_sides={p['side'] for p in pending if not p.get('cancel_requested_ts')}
    selected_side=0
    direction_reason='NO_VALID_ICT_SCENARIO'
    if len(active_sides)>1:
        direction_reason='LEGACY_OPPOSING_OPEN_POSITIONS_MANAGE_ONLY'
        can_place=False
    elif active_sides:
        # User's single-thesis policy: once a position is OPEN, no additional
        # same-side or opposite-side LIMIT may be admitted. The available second
        # slot is NOT an instruction to pyramid. Independently watch whether
        # the existing ICT thesis is losing confirmed 15m/1H structure.
        selected_side=next(iter(active_sides))
        can_place=False
        direction_reason='ACTIVE_POSITION_MONITOR_ONLY'
        for old in pending:
            if not old.get('cancel_requested_ts'):
                request_cancel(state,old,'OPEN_POSITION_CANCEL_OTHER_LIMITS',s.now)
    else:
        proposed_side,ranked,direction_reason=policy.choose(valid,s.price,cfg.tick,cfg.conflict_margin)
        if len(pending_sides)>1:
            # A state created by the previous unlimited/opposing router is
            # resolved by preserving its highest-priority existing thesis.
            keeper=max((p for p in pending if not p.get('cancel_requested_ts')),
                       key=lambda p:policy.ranking(p,s.price,cfg.tick))
            selected_side=keeper['side']
            direction_reason='LEGACY_OPPOSING_LIMITS_CANCELLING'
            for p in pending:
                if p['side']!=selected_side:
                    request_cancel(state,p,'PORTFOLIO_DIRECTION_CONFLICT',s.now)
            can_place=False
        elif pending_sides:
            current=next(iter(pending_sides))
            incumbent=max((p for p in pending if p['side']==current and not p.get('cancel_requested_ts')),
                           key=lambda p:policy.ranking(p,s.price,cfg.tick))
            if False and direction_reason=='DIRECTION_AMBIGUOUS_NO_TRADE':
                # A newly ambiguous two-way thesis should not leave a stale
                # limit waiting to fill. Cancel causally on the next boundary.
                for stale in pending:
                    if not stale.get('cancel_requested_ts'):
                        request_cancel(state,stale,'DIRECTION_AMBIGUOUS_CANCEL_PENDING',s.now)
                can_place=False
                selected_side=current
                direction_reason='DIRECTION_AMBIGUOUS_CANCEL_PENDING'
            challenger=next((p for p in valid if p['side']==proposed_side),None)
            improvement=(policy.ranking(challenger,s.price,cfg.tick)[0]
                         -policy.ranking(incumbent,s.price,cfg.tick)[0]) if challenger else 0.
            if (can_place and proposed_side in (-1,1) and proposed_side!=current and
                arbitration.directional_lock(pending,active,proposed_side,s,cfg,sys.modules[__name__]).get('cancel_old_first')):
                # Switch to a materially stronger thesis ONLY after the
                # old limit cancellation has become effective on a 3m boundary.
                for p in pending:
                    if p['side']!=proposed_side and not p.get('cancel_requested_ts'):
                        request_cancel(state,p,'STRUCTURE_FLIPPED',s.now)
                direction_reason='DIRECTION_SWITCH_AWAITING_CANCEL'
                can_place=False
                selected_side=current
            elif can_place:
                selected_side=current
                direction_reason='PENDING_DIRECTION_LOCK'
            elif arbitration_audit.get('decision')=='REQUEST_REPLACEMENT':
                selected_side=current
                direction_reason='BETTER_LIMIT_CANCELLING_BEFORE_REPLACEMENT'
        elif proposed_side:
            selected_side=proposed_side
        # No existing thesis and tied scores => WAIT, not simultaneous hedges.
    analysis['position_reviews'] = [dict(order_id=p.get('id'), **position_monitor.review(p,s))
                                    for p in active_plans(state)]
    analysis['direction_policy']={'side':selected_side,'reason':direction_reason,
        'meta_rejected':meta_rejected,
        'meta_status':fitted['status'],
        'pending':len(pending),'open_positions':len(active),
        'long_ready':sum(p['side']==1 for p in valid),
        'short_ready':sum(p['side']==-1 for p in valid),
        'arbitration_decision':arbitration_audit.get('decision'),
        'occupied_slots':occupied_slots(state),
        'reserved_pending':len(pending_plans(state)),
        'free_slots':max(0,order_limit-occupied_slots(state)-len(pending_plans(state)))}
    state['portfolio_policy']=state['pending_policy']
    if can_place and selected_side:
        # Only one order may reserve a new entry, including pending cancellation.
        plans=[p for p in valid if p['side']==selected_side]
        plans=sorted(plans,key=lambda p:policy.ranking(p,s.price,cfg.tick),reverse=True)
        intent=state.get('portfolio_rotation_intent')
        if intent and not any(p.get('cancel_requested_ts') for p in pending_plans(state)):
            # Revalidate right NOW; a plan saved in a previous cycle is never
            # authorised to trade without current closed-candle/price proof.
            matching=next((p for p in plans if p.get('event_key')==intent.get('candidate_event_key')
                           and execution_atlas.passive_at_open(p,s.price,cfg.tick)),None)
            if matching:
                plans=[matching]+[p for p in plans if p is not matching]
            else:
                state.pop('portfolio_rotation_intent',None)
        for top in plans:
            existing=pending_plans(state)
            # Each waiting limit may fill before the next 15m scan. Count it
            # together with already open/uncertain positions, not separately.
            if occupied_slots(state)+len(existing)>=order_limit:
                break
            if any(p.get('cancel_requested_ts') for p in existing):
                break
            busy=open_plans(state)
            keys={top['event_key'],*top.get('parent_keys',[])}
            live_keys={key for p in busy for key in (p['event_key'],*p.get('parent_keys',[]))}
            tolerance=max(3*cfg.tick,.15*atr(s.candles.get('3m',[])))
            if keys.intersection(state['used_events']) or keys.intersection(live_keys):
                continue
            if any(p['side']!=top['side'] for p in busy):
                break
            if any(p['side']==top['side'] and abs(p['entry']-top['entry'])<=tolerance for p in busy):
                continue
            # The second position must express a distinct ICT thesis, not a
            # duplicate entry under a different detector name/timeframe.
            if any(not policy.compatible(top,incumbent,cfg.tick) for incumbent in busy):
                continue
            if any(trade_dependencies.related(top,incumbent,cfg.tick) for incumbent in busy):
                continue
            if existing and policy.ranking(top,s.price,cfg.tick)[0] < \
                    policy.ranking(existing[0],s.price,cfg.tick)[0]-cfg.conflict_margin:
                continue
            plan=copy.deepcopy(top)
            plan.setdefault('queue_execution_policy','PASSIVE_ARM_V2')
            if plan.get('outcome_forecast'):
                lifecycle_forecast.select_full_exit(plan,s,cfg,sys.modules[__name__])
            else:
                plan['outcome_forecast']={'status':'DEFERRED_MODEL_BUDGET','calibrated':False,
                    'as_of_ts':s.now,'entry_gate_enabled':False}
            plan['decision_forecast']=copy.deepcopy(plan.get('outcome_forecast') or {})
            plan['placed_ts']=s.now
            plan.setdefault('effective_ts',((s.now+TF['3m']-1)//TF['3m'])*TF['3m'])
            plan.setdefault('target_cancel_armed',plan['side']*(s.price-plan['tp'])<0)
            park_pending(state,pending_plans(state)+[plan])
            bind_order_numbers(state)
            selected.append(plan)
            intent=state.get('portfolio_rotation_intent')
            if intent and intent.get('candidate_event_key')==plan.get('event_key'):
                emit(state,'PORTFOLIO_ROTATION_COMMITTED',s.now,
                     replacement_id=plan['id'],replaced_id=intent['replaced_id'],
                     decision_ts=intent['decided_ts'],no_third_limit=True)
                state.pop('portfolio_rotation_intent',None)
            emit(state,'PLACED',s.now,order_id=plan['id'],side=plan['side'],entry=plan['entry'],
                 stop=plan['stop'],tp=plan['tp'],effective_ts=plan['effective_ts'])
            if plan.get('action_type')=='ACTIVE_ENTRY':
                park_pending(state,[p for p in pending_plans(state) if p['id']!=plan['id']])
                book={**state,'pending':plan,'active':None,'reconciliation':None}
                direct = plan.get('direct_entry') or {}
                reason = ('CURRENT_QUOTE_MOMENTUM' if direct.get('pattern') in ('BREAKOUT', 'ZONE_REACTION')
                          else 'CURRENT_QUOTE_CATCH_UP')
                end_order(book,plan,'FILLED',reason,s.now)
                quote=Candle(s.now,plan['entry'],plan['entry'],plan['entry'],plan['entry'])
                filled=bzu_policy.first_fill(plan,quote,sys.modules[__name__],cfg,60_000)
                filled['fill_time_status']='CURRENT_QUOTE_AT_REPORT'
                filled['last_bar']=(s.now//60_000)*60_000
                filled['last_execution_step_ms']=60_000
                for number,view in slot_views(state):
                    if not view.get('active') and not view.get('reconciliation'):
                        view['active']=filled
                        store_slot(state,number,view)
                        break
                action='ACTIVE_ENTRY'
                break
            action='PLACE_LIMIT'
    elif can_place and not selected_side and valid:
        action='DIRECTION_CONFLICT'
    elif can_place and meta_rejected and not valid:
        action='META_REJECTED'
    elif direction_reason in ('LEGACY_OPPOSING_LIMITS_CANCELLING','DIRECTION_SWITCH_AWAITING_CANCEL','DIRECTION_AMBIGUOUS_CANCEL_PENDING'):
        action='CANCEL_LIMIT'
    # Publish only plans that survived admission and the current portfolio lock.
    # A valid reaction watch can be actionable without a placed paper order.
    watch = []
    if watch_allowed and not open_plans(state):
        watches = [p for p in confirmed if p.get('action_type') != 'ACTIVE_ENTRY'
                   and p.get('setup') != 'SESSION_MEAN_RECLAIM'
                   and (entry_mode == 'LIVE_REACTION' or p['zone_quality']['entry_condition'] != 'RETEST'
                        or (entry_mode == 'HYBRID' and p['zone_quality']['tactic'] == 'REACTION_M5'))]
        watch_side, _, _ = policy.choose(watches, s.price, cfg.tick, cfg.conflict_margin)
        watch = [p for p in watches if p['side'] == watch_side]
        if watch:
            action = 'WATCH_REACTION'
    analysis['reaction_plans'] = copy.deepcopy(watch)
    analysis['admitted_plans'] = copy.deepcopy(selected + watch)
    # Convert the market-ready state into the ACTUAL portfolio decision, so a
    # suppressed duplicate or a full book is never described as unexplained.
    tolerance=max(3*cfg.tick,.15*atr(s.candles.get('3m',[])))
    live=open_plans(state)
    for row in analysis.get('setup_matrix',[]):
        if row.get('status')!='LIMIT_READY':
            continue
        exact=next((p for p in live if row.get('event_key')==p.get('event_key')),None)
        duplicate=next((p for p in live if row.get('side')==p['side'] and row.get('level')
                        and abs(row['level']-p['entry'])<=tolerance),None)
        if exact or duplicate:
            row.update(status='FOLLOWED',reason='LEVEL_ALREADY_FOLLOWED',
                       followed_id=(exact or duplicate)['id'])
        elif occupied_slots(state)>=2:
            row.update(status='WAIT_CAPACITY',reason='SLOTS_FULL')
        elif action in ('DIRECTION_CONFLICT','DAILY_PAUSE','SESSION_CLOSED','DATA_HOLD'):
            row.update(status='BLOCKED',reason=action)
    if analysis.get('setup_matrix'):
        analysis['watch']=[w for w in analysis.get('watch',[]) if w.get('status')!='FOLLOWED']
    sides=None  # Existing long positions must not hide short candidates.
    selected_row=next((row for row in analysis.get('setup_matrix',[]) if selected
                       and row.get('signal_id')==selected[0]['id']),None)
    analysis['best_scenario']=selected_row or scanner.best_scenario(analysis.get('watch',[]),s.price,sides)
    if analysis['best_scenario']:
        best=analysis['best_scenario']
        best['outcome_forecast']=next((p.get('outcome_forecast') for p in live
                                      if p['id']==best.get('signal_id')),None)
    modeled_waiting=sorted(pending_plans(state),key=lambda p:execution_atlas.entry_rank(p,s.price,scanner,cfg.tick),reverse=True)
    for index,plan in enumerate(modeled_waiting):
        if index<6:
            plan['outcome_forecast']=lifecycle_forecast.forecast(plan,s,cfg,sys.modules[__name__])
        elif plan.get('outcome_forecast',{}).get('as_of_ts')!=s.now:
            plan['outcome_forecast']={'status':'DEFERRED_MODEL_BUDGET','calibrated':False,
                'as_of_ts':s.now,'entry_gate_enabled':False}
    state['position_reviews']=copy.deepcopy(analysis.get('position_reviews',[]))
    state['best_scenario']=copy.deepcopy(analysis['best_scenario'])
    state['direct_strategy_diagnostic']=copy.deepcopy(analysis.get('direct_strategy',{}))
    state['last_run_ts'] = s.now
    state['config_hash'] = config_hash
    state['runtime_config']=config_dict(cfg)
    state['include_commission']=cfg.include_commission
    state['price_tick']=cfg.tick
    state['version'] = VERSION
    state['entry_mode'] = entry_mode
    state['ledger_revision'] = 2
    cutoff = s.now-2*86_400_000
    state['used_events'] = {k:v for k,v in state['used_events'].items() if v>=cutoff}
    state['level_cooldown'] = {k:v for k,v in state.get('level_cooldown',{}).items() if v>s.now}
    # Keep IDs for every followed plan, not only the newly selected candidate.
    summary = {'ts':s.now,'time':iso(s.now),'version':VERSION,'action':action,'price':s.price,
               'plan_id':selected[0]['id'] if selected else None,
               'placed_ids':[p['id'] for p in selected],
               'followed_ids':[p['id'] for p in open_plans(state)],
               'occupied_slots':occupied_slots(state),'max_slots':2,
               'rejections':analysis.get('rejections',{}),'health':analysis['health'],'daily':daily,
               'daily_entry_limits_enabled':False,
               'detectors':analysis.get('detectors',[]),'raw_plans':analysis.get('raw_plans',0),
               'registered_setups':analysis.get('registered_setups',24),
               'timeframe_evaluations':analysis.get('timeframe_evaluations',0),
               'funnel':{**analysis.get('funnel',{}),'placed':len(selected)},
               'admission_by_side':analysis.get('admission_by_side',{}),
               'direct_strategy':analysis.get('direct_strategy',{}),
               'direction_policy':analysis.get('direction_policy',{}),
               'portfolio_arbitration':analysis.get('portfolio_arbitration',{})}
    previous=state.get('last_scan_observed_ts')
    summary['scan_gap_minutes']=(s.now-previous)/60_000 if previous else None
    state['last_scan_observed_ts']=s.now
    state['signals'].append(summary)
    state['signals'] = state['signals'][-1000:]
    state['watch'] = analysis.get('watch',[])
    state['setup_matrix'] = analysis.get('setup_matrix',[])
    events = sorted(state['events'][start:],key=lambda e:e['ts'])
    return {'action':action,'events':events,'analysis':analysis,'selected':selected[0] if selected else None,
            'selected_plans':selected,'daily':daily,'occupied_slots':occupied_slots(state)}


def statistics(trades: list[dict], key: str = "net_r") -> dict:
    vals = [finite(t[key]) for t in trades if t.get(key) is not None]
    n, wins = len(vals), sum(v > 0 for v in vals)
    losses = -sum(v for v in vals if v < 0)
    gains = sum(v for v in vals if v > 0)
    lower = 0.0
    if n:
        p, z = wins/n, 1.96
        lower = (p+z*z/(2*n)-z*math.sqrt(p*(1-p)/n+z*z/(4*n*n)))/(1+z*z/n)
    return {"trades": n, "wins": wins, "losses": sum(v < 0 for v in vals),
            "breakeven": sum(v == 0 for v in vals), "win_rate": wins/n if n else None,
            "wilson_lower_95": lower if n else None, "net_r": sum(vals),
            "expectancy_r": mean(vals) if n else None,
            "profit_factor": gains/losses if losses else None,
            "max_drawdown_r": drawdown(vals)}


def trade_entry_version(trade: dict) -> str:
    """A close after a code update still belongs to its original entry cohort."""
    return (trade.get('bot_version_at_entry') or trade.get('entry_version')
            or trade.get('version') or 'UNKNOWN')


def statistics_by_entry_version(trades: list[dict]) -> dict:
    groups = {}
    for trade in sorted(trades, key=lambda t: t.get('closed_ts', 0)):
        groups.setdefault(trade_entry_version(trade), []).append(trade)
    return {version: statistics(rows) for version, rows in groups.items()}


def drawdown(values: list[float]) -> float:
    equity = peak = worst = 0.0
    for v in values:
        equity += v
        peak = max(peak, equity)
        worst = max(worst, peak-equity)
    return worst


def audit_journal(path: Path) -> dict:
    data = read_json(path)
    trades = data.get("trades", [])
    measured = []
    for t in trades:
        value = t.get("net_r", t.get("pnl_r"))
        if value is not None:
            measured.append({**t, "net_r": value})
    order_counts, reasons, versions = {}, {}, {}
    for o in data.get("limit_orders", []):
        k = str(o.get("status", "UNKNOWN"))
        order_counts[k] = order_counts.get(k, 0)+1
    for t in trades:
        k = str(t.get("close_reason", "UNKNOWN"))
        reasons[k] = reasons.get(k, 0)+1
        v = str(t.get("bot_version_at_entry", t.get("version", "UNKNOWN")))
        versions.setdefault(v, []).append(t)
    arming = data.get("limit_arming_statistics", {})
    return {"source": path.name, "statistics": statistics(measured), "limit_orders": order_counts,
            "v11_statistics": statistics(data.get("v11", {}).get("trades", [])),
            "close_reasons": reasons,
            "mfe_ge_1r_but_net_loss": sum(t.get("mfe_r", 0) >= 1 and t["net_r"] < 0 for t in measured),
            "arming_refusal_counts": arming.get("refusal_counts", {}),
            "by_version": {v: statistics([{**t, "net_r": t.get("net_r", t.get("pnl_r"))}
                                          for t in rows]) for v, rows in versions.items()},
            "limitation": "Mixed strategy versions; no OHLC dataset. Cannot backtest v11 from this journal."}


def display_order(state: dict, order_id: str) -> str:
    row = next((p for p in open_plans(state)+state.get('trades',[])+state.get('orders',[])
                if p['id']==order_id),{})
    return row.get('display_id') or '#'+order_id[:6]


def local_time(ts: int, cfg: Config) -> str:
    return datetime.fromtimestamp(ts/1000,ZoneInfo(cfg.timezone)).strftime('%H:%M')


def target_line(p: dict) -> str:
    import notification_report
    return notification_report.targets(p)


def message_event(state: dict, event: dict, cfg: Config) -> Optional[str]:
    kind = event['kind']
    label = display_order(state,event.get('order_id',''))
    at = local_time(event['ts'],cfg)
    if kind=='PLACED':
        return f"🆕 {at} {label} Лімітний сигнал активовано: вхід {event['entry']:.6g}"
    if kind=='CLOSED':
        trade = next((t for t in reversed(state['trades']) if t['id']==event['order_id']),{})
        reason = event.get('reason')
        text = {'STOP':'стоп','STOP_GAP':'стоп із розривом ціни',
                'TAKE_PROFIT':'тейк-профіт','ADAPTIVE_FULL_EXIT':'повний вихід за зміною умов','INTRADAY_TIME_EXIT':'кінець часу угоди',
                'POST_DEADLINE_THESIS_INVALIDATED':'вихід після втрати торгового сценарію'}.get(reason,'завершення')
        value = event['net_r']
        outcome = 'прибуток' if value>0 else 'збиток' if value<0 else 'беззбитково'
        achieved = [name for name in ('TP1','TP2','TP3') if trade.get(name.lower()+'_hit')]
        details = (', '.join(achieved)+' досягнуто; ' if achieved else '') if trade.get('exit_policy') != 'SINGLE_FULL_EXIT' else ''
        uncertainty = ' • порядок SL/TP у свічці невідомий, узято стоп' if event.get('ambiguous_ohlc') else ''
        emoji='✅' if value>0 else '🔴' if value<0 else '⚪'
        return f"{emoji} {at} {label} ЗАКРИТО: {value:+.2f}R — {outcome}\n   {details}{text}{uncertainty}"
    if kind=='FILLED':
        return f"🟢 {at} {label} ЦІНА ПРОЙШЛА ЛІМІТ — супровід сигналу розпочато"
    if kind in ('TP1','TP2','TP3'):
        return None  # Legacy partial events remain in the ledger, not in current alerts.
    if kind=='PAPER_STOP_AMENDED':
        return f"🛡️ {at} {label} Перенесення стопа на {event['stop']:.6g} з {local_time(event['effective_ts'],cfg)}"
    if kind in ('CANCELLED','EXPIRED'):
        text = 'скасовано' if kind=='CANCELLED' else 'строк очікування завершився'
        reasons = {'CONFIRMED_HTF_FLIP':'змінився напрямок', 'STRUCTURE_INVALIDATED_NOW':'сетап утратив чинність',
                   'TARGET_REACHED_WITHOUT_FILL':'ціль досягнута без входу'}
        suffix = '; '+reasons[event['reason']] if event.get('reason') in reasons else ''
        return f"⚪ {at} {label} Ліміт {text}, угода НЕ відкрилася{suffix}"
    if kind=='CANCEL_REQUESTED':
        return f"{at} {label} Скасувати невиконаний ліміт; перевірити, чи не було входу"
    if kind=='UNRESOLVED':
        return f"{at} {label} Виконання не підтверджене: порядок подій у свічці невідомий"
    if kind=='UNCONFIRMED_RELEASED':
        return f"{at} {label} Строк супроводу минув; результат не підтверджений і не включений у win rate"
    return None


REASON_UA = {
    'ROUND_LEVEL_TARGET_COLLAPSED':'після корекції круглих рівнів TP1/TP2 не мають коректного порядку',
    'ROUND_LEVEL_RR_TOO_LOW':'після корекції перед круглим рівнем потенціал TP/SL недостатній',
    'IMPULSE_CONFIRMATION_REQUIRED':'після імпульсу чекаємо закриту свічку гальмування або відкупу',
    'ZONE_ORIGIN_BOS_REQUIRED':'імпульс із цієї зони не підтвердив злам структури',
    'ZONE_ORIGIN_HISTORY_MISSING':'бракує історії для перевірки походження зони',
    'ICT_SWEEP_OR_BOS_FVG_REQUIRED':'потрібне зняття ліквідності або імпульсний BOS зі свіжим FVG',
    'CONFLUENCE_BELOW_THRESHOLD':'оцінка нижче порогу допуску',
    'LOCAL_STRUCTURE_CONFLICT':'15m проти старшого тренду; чекаємо CHoCH або досягнення HTF зони',
    'SAME_SIDE_LOSS_COOLDOWN':'після стопа цей напрямок на паузі до години або нового A+ sweep',
    'OIL_OVERNIGHT_BLACKOUT':'нічне вікно ризику 22:00–01:00 UTC',
    'EIA_INVENTORY_WINDOW':'пауза навколо публікації запасів EIA',
    'US_OPEN_CONFIRMATION_REQUIRED':'чекаємо завершення перших 15 хвилин вікна Нью-Йорка',
    'EXECUTION_CONTROL_REVERSED':'підтверджена зміна локального напрямку після виставлення',
    'LEVEL_PASSED_WHILE_UNARMED':'ціна пройшла рівень, поки вхід був недоступний',
    'DAILY_RISK_LIMIT_REACHED':'історичне скасування за старим денним обмеженням',
    'CAPITULATION_RECOVERY_NOT_CONFIRMED':'після капітуляції ще немає підтвердженого відновлення',
    'CAPITULATION_RECOVERY_LOST':'відновлення після капітуляції зламане до входу',
    'WAIT_CONFIRMED_BREAK_FOR_LIMIT':'чекаємо підтвердженого пробою перед лімітним ретестом',
    'SLOTS_FULL':'відкриті дві угоди; новий вхід після закриття однієї',
    'EXISTING_OPPOSITE_PLAN':'вже супроводжуємо протилежний напрямок',
    'DIRECTION_CONFLICT':'протилежні сценарії мають близьку оцінку',
    'META_REJECTED':'метамодель відхилила сценарії; немає підтвердженої переваги',
    'DAILY_PAUSE':'денний ліміт ризику або кількості входів',
    'SESSION_CLOSED':'наступна торгова сесія для 15m входу',
    'DATA_HOLD':'відновлення повних актуальних даних',
    'LIMIT_WOULD_CROSS_SPREAD':'поточні ASK/BID не дозволяють пасивний ліміт',
    'FORMATION_VOLUME_REQUIRED':'імпульсу бракує підтвердження обсягом',
    'RISK_TOO_WIDE':'стоп за структурою перевищує допустимий ризик',
    'RISK_BELOW_FLOOR':'стоп вужчий за мінімальну відстань',
    'STRUCTURAL_TP1_REQUIRED':'перша ціль не відповідає правилам плану',
    'STRUCTURAL_TP2_REQUIRED':'бракує структурної цілі для залишку позиції',
    'LEGACY_MICRO_RISK':'лімітка зі старим стопом замінюється новим розрахунком',
    'PENDING_SINGLE_LIMIT_CAP':'у журналі дозволено одну лімітку',
    'MACRO_STOP_ANCHOR_REQUIRED':'бракує підтвердженого макрорівня для стопа',
    'MACRO_REVERSAL_REQUIRED':'контртрендовий вхід потребує відбою від екстремуму 1H, 4H або попередньої доби',
    'ORGANIC_TARGET_RANGE':'цілі поза свінговим діапазоном R',
    'OIL_VOLATILITY_UNAVAILABLE':'бракує закритих свічок для розрахунку годинного ATR',
    'SESSION_MEAN_LIMIT_DISABLED':'лімітні входи від середньої сесії вимкнено',
    'MICRO_ZONE_EXCLUDED':'мікрозона без рівня старшого таймфрейму',
    'RANGE_ZONE_TOO_CLOSE':'зона занадто близько до ціни в діапазоні',
    'LIMIT_UNREACHABLE':'рівень далі допустимої відстані у ATR',
    'EXHAUSTED_MOVE':'перша ціль уже пройдена без входу',
    'ENTRY_TOO_CLOSE':'ціна вже надто близько до рівня для нового ліміту',
    'INVALID_STRUCTURAL_FALSIFIER':'немає коректного рівня скасування сетапу',
    'ANCHOR_INVALIDATED':'структура сетапу порушена',
    'LEVEL_ALREADY_VISITED_AFTER_OBSERVATION':'рівень уже відвідано після виявлення',
    'STRUCTURAL_STOP_TOO_WIDE':'структурний стоп перевищує допустимий ризик',
    'NEAREST_TARGET_TOO_CLOSE_AFTER_COSTS':'потенціал TP відносно SL нижчий за мінімум',
    'TARGET_RR_TOO_LOW':'потенціал TP відносно SL нижчий за мінімум',
    'TARGET_DISTANCE_TOO_SMALL':'найближча структурна ціль ближча за мінімальний TP1 2.0%',
    'STRONG_OPPOSING_ZONE_BEFORE_TP':'сильна протилежна зона перекриває шлях до TP1',
    'HTF_REJECTED_NEAR_LIQUIDITY':'повторне відхилення від близької 1H ліквідності перед входом',
    'ENTRY_INSIDE_STRONG_OPPOSING_ZONE':'вхід усередині сильної протилежної зони',
    'ANCHOR_EXPIRED_AWAITING_NEW_EVENT':'строк рівня минув; потрібна нова підтверджена реакція або нова сесія',
    'NO_STRUCTURAL_TARGET':'немає підтвердженої цілі на графіку',
    'COMBINED_CONTEXT_SCORE_LOW':'оцінка контексту нижча порогу',
    'COSTS_CONSUME_EDGE':'витрати надто великі відносно потенційного прибутку',
    'BOTH_HTF_AGAINST_WITHOUT_CONFIRMED_REVERSAL':'1h і 4h проти входу, розворот не підтверджено',
    'HTF_REVERSAL_NOT_CONFIRMED':'розворот проти 1h і 4h не підтверджено',
    'NO_CONFIRMED_3M_CONTROL':'немає контролю на молодшому графіку',
    'NO_CONFIRMED_DEPARTURE':'немає підтвердженого відходу від рівня',
    'NO_15M_ACCEPTANCE':'пробій на основному графіку не підтверджено',
    'REVERSAL_NOT_CONFIRMED':'зміна структури ще не підтверджена',
    'CONTINUATION_AGAINST_HTF':'продовження тренду суперечить старшим графікам',
    'RANGE_ENTRY_NOT_AT_EDGE':'точка входу посередині діапазону',
    'SHOCK_STILL_ACTIVE':'чекаємо завершення різкого стрибка волатильності',
    'RETURN_PATH_TOO_LONG':'ціна надто далеко від рівня',
        'ANCHOR_TOO_OLD_TO_PLACE':'сетап утратив актуальність',
    'ORDER_LIFETIME_BELOW_REPORT_CADENCE':'до завершення сесії замало часу',
    'LEVEL_OR_EVENT_COOLDOWN':'цей рівень уже використаний; чекаємо новий сетап',
    'ANCHOR_COOLDOWN':'рівень ще на паузі',
    'STOP_INSIDE_NOISE':'стоп усередині цінового шуму',
    'DETECTOR_ERROR':'помилка аналізу цього сетапу',
    'DEPENDENT_ENTRY_ALREADY_RESERVED':'близький залежний вхід уже займає місце',
    'ICT_LIQUIDITY_SWEEP_NOT_CONFIRMED':'ліквідність позначено, але зняття та повернення ціни ще немає',
    'BETTER_LIMIT_CANCELLING_BEFORE_REPLACEMENT':'очікуємо підтвердження скасування слабшого ліміту',
    'BETTER_SAME_DIRECTION_ICT_CANDIDATE':'сильніший незалежний ICT-сценарій; спершу скасовуємо старий ліміт',
    'ICT_RANGE_EDGE_WITHOUT_REJECTION':'рівень діапазону є, але підтвердженої реакції поки немає',
    'ICT_SESSION_HAS_NO_PRICE_TRIGGER':'час сесії сам по собі не є сигналом ICT',
    'ICT_VALUE_RECLAIM_UNCONFIRMED':'рівень вартості без підтвердженого reclaim і імпульсу',
    'ICT_CONTINUATION_DEPARTURE_MISSING':'відкат без підтвердженого початку руху',
    'ICT_SWEEP_STRUCTURE_SEQUENCE_MISSING':'немає послідовності зняття ліквідності та зміни структури',
    'ICT_MOMENTUM_IMBALANCE_MISSING':'імпульс без підтвердженої незаповненої FVG',
    'ICT_BREAK_CLOSE_MISSING':'немає підтвердженого пробою закритою свічкою',
    'ICT_ACCEPTANCE_NOT_CONFIRMED':'немає підтвердженого прийняття пробитого рівня',
    'ICT_STRUCTURE_SHIFT_NOT_CONFIRMED':'немає підтвердженого MSS/BOS',
    'ICT_FAILED_BREAK_NOT_PROVEN':'немає доказу відмови від пробою',
    'ICT_RECOVERY_NOT_CONFIRMED':'відновлення після капітуляції не підтверджено',
    'ICT_RECLAIM_LACKS_LIQUIDITY_OR_IMBALANCE':'немає підтвердженого зняття ліквідності або FVG',
    'PORTFOLIO_DIRECTION_CONFLICT':'скасування: в портфелі не можна одночасно вести LONG і SHORT',
    'LEGACY_PENDING_EXCESS_CAP_2':'скасування зайвого ліміту, залишаємо максимум два',
    'BETTER_OPPOSING_SCENARIO':'скасування: протилежний сценарій тепер істотно сильніший',
    'ACTIVE_POSITION_MONITOR_ONLY':'супроводжуємо угоду; нові ліміти не відкриваємо',
    'OPEN_POSITION_CANCEL_OTHER_LIMITS':'відкрито позицію; решту лімітів скасовуємо',
    'ACTIVE_POSITION_LOCK_NO_NEW_PLACEMENT':'супроводжуємо відкриту позицію; нові ліміти на паузі (стара політика)',
    'ACTIVE_POSITION_SECOND_ENTRY_REVIEW':'перевіряємо другий незалежний ICT-вхід у тому самому напрямку',
    'ACTIVE_POSITION_OPPOSITE_PENDING_RECONCILIATION':'скасовуємо протилежний ліміт перед новим входом',
    'ACTIVE_POSITION_TWO_SLOT_CAPACITY_FULL':'обидва місця зайняті позиціями чи лімітами; новий ордер не додаємо',
    'PENDING_DIRECTION_LOCK':'зберігаємо обраний напрямок очікуваних лімітів',
    'DIRECTION_SELECTED_NARROW_EDGE':'LONG і SHORT близькі за оцінкою; обрано лише один напрямок',
    'DIRECTION_AMBIGUOUS_NO_TRADE':'оцінки LONG і SHORT занадто близькі: не відкривати угоду',
    'DIRECTION_AMBIGUOUS_CANCEL_PENDING':'напрямок став неоднозначним, старий ліміт скасовується',
    'META_LOW_PROBABILITY_OR_NONPOSITIVE_EXPECTANCY':'оцінена перевага входу недостатня',
    'LEGACY_OPPOSING_OPEN_POSITIONS_MANAGE_ONLY':'виявлено старі протилежні відкриті позиції; нові входи заблоковані',
}


def expected_line(stat: dict, forecast=None) -> str:
    n=stat.get('observations',0)
    if n and stat.get('estimate') is not None:
        scope='сетап' if stat.get('scope')=='setup' else 'сім’я сетапів'
        lower=stat.get('lower_95')
        return (f"📈 Оцінка win rate: {stat['estimate']*100:.1f}% • {scope}, n={n}"
                +(f" • нижня межа 95%: {lower*100:.1f}%" if lower is not None else '')
                +' • історична оцінка, ще не перевірена на відкладених даних')
    model=forecast or {}
    if model.get('status')=='MODEL_ONLY':
        minutes=model['horizon_minutes']
        period=f"{minutes//60} год {minutes%60} хв" if minutes>=60 and minutes%60 else f"{minutes//60} год" if minutes>=60 else f"{minutes} хв"
        return (f"🔮 TP1 до SL: ≈{model['tp1_before_sl']*100:.1f}% • цінова модель після входу"
                +f"\n📊 Основа: {model['closed_bars']} закритих {model['source_timeframe'].lower()} свічок • горизонт {period}"
                +f"\n📈 Історія: {n} завершених сценаріїв • модель ще не перевірена")
    need={'NEEDS_SL_TP':'Для прогнозу потрібні структурні SL і TP',
          'INVALID_BARRIERS':'Рівні SL/TP ще не дають коректного прогнозу',
          'NEEDS_CLOSED_HISTORY':f"Для прогнозу потрібно {price_forecast.MINIMUM_BARS} закритих свічок",
          'INVALID_HORIZON':'Для прогнозу потрібен допустимий строк супроводу'}
    if model:
        return '🧮 '+need.get(model.get('status'),'Прогноз ще готується')+f" • історія {n} завершених"
    return f"📈 Історія: {n} завершених сценаріїв • win rate ще не перевірений"


def report_deadline(ts: int, now: int, cfg: Config) -> str:
    moment=datetime.fromtimestamp(ts/1000,ZoneInfo(cfg.timezone))
    current=datetime.fromtimestamp(now/1000,ZoneInfo(cfg.timezone))
    return moment.strftime('%H:%M' if moment.date()==current.date() else '%d.%m %H:%M')


def build_message(state: dict, s: Snapshot, result: dict, cfg: Config) -> str:
    import notification_report
    return decorate_message(notification_report.build(state, s, result, cfg, sys.modules[__name__]))


def decorate_message(message):
    """Keep source status emojis; add labels only to older emoji-less reports.

    Idempotent: the body is decorated on creation and again on queue delivery.
    Dropping all emojis formerly erased the ⏳/🔵/✅ lifecycle indicators.
    """
    icons = [('Ціна зараз:', '💰'), ('Ціна:', '💰'),
             ('Найкращий сценарій:', '🏆'), ('Готовність до входу', '🧩'),
             ('СУПРОВІД —', '🔵'), ('ОЧІКУВАННЯ —', '⏳'),
             ('ОЧІКУЄМО ЛІМІТ —', '🟡'), ('СПОСТЕРІГАЄМО', '👀'),
             ('ЗАКРИТІ УГОДИ', '✅'),
             ('Структура: без', '🟢'), ('Структура: можливе', '🟠'),
             ('Структура: ризик', '🔴'), ('Структура: недостатньо', '⚪')]

    def is_icon(char):
        point = ord(char)
        return (0x1F000 <= point <= 0x1FAFF or 0x2300 <= point <= 0x23FF
                or 0x2600 <= point <= 0x27BF or point in (0xFE0F, 0x200D, 0x20E3))

    output = []
    for line in message.split('\n'):
        stripped = line.strip()
        leading = stripped.split(' ', 1)[0]
        if stripped and not any(is_icon(ch) for ch in leading):
            icon = next((mark for start, mark in icons if stripped.startswith(start)), None)
            if icon:
                stripped = icon + ' ' + stripped
        output.append(stripped)
    return '\n'.join(output)


def read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    # Corruption must fail visibly, never silently create a second position.
    with path.open(encoding="utf-8") as handle:
        data = json.load(handle)
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object: {path.name}")
    return data


def atomic_write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=path.name+".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            # On large persistent ledgers indentation doubles disk and Git I/O.
            # Compact encoding preserves all keys, numbers, order and timestamps.
            json.dump(data, handle, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


@contextmanager
def locked(path: Path):
    # macOS/Linux; lock file persists, advisory lock does not persist after crash.
    import fcntl
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError("Another bot cycle is already running")
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def load_state(path: Path, cfg: Config) -> dict:
    raw = read_json(path)
    if not raw:
        return new_state(cfg)
    if raw.get("schema") != SCHEMA:
        raw = migrate_state(raw,cfg)
    for key in ("trades", "orders", "events", "signals", "notification_queue", "used_events"):
        if key not in raw:
            raise ValueError(f"State missing {key}; refusing reset")
    raw.setdefault('pending_orders',[])
    if not isinstance(raw['pending_orders'],list):
        raise ValueError('Invalid pending queue; refusing reset')
    if 'second_slot' not in raw:
        raw['second_slot'] = {'pending':None,'active':None,'reconciliation':None}
    if not isinstance(raw['second_slot'],dict):
        raise ValueError('Invalid second slot; refusing reset')
    for key in ('pending','active','reconciliation'):
        if key not in raw['second_slot']:
            raise ValueError('Incomplete second slot; refusing reset')
    # Preserve the rare older state holding both a position and one limit.
    if raw.get('pending') and raw.get('active') and not any(raw['second_slot'].values()):
        raw['second_slot']['pending'], raw['pending'] = raw['pending'], None
    for _,view in slot_views(raw):
        if view.get('pending') and view.get('active'):
            raise ValueError('Two plans in one slot; refusing to drop either')
        if view.get('reconciliation') and (view.get('pending') or view.get('active')):
            raise ValueError('Uncertain and open plan share one slot; reconciliation required')
    plans = open_plans(raw)
    if len({p['id'] for p in plans}) != len(plans):
        raise ValueError('Duplicate open order identity; refusing reset')
    raw.setdefault('ledger_revision',2)
    raw.setdefault('order_sequence',0)
    bind_order_numbers(raw)
    return raw


def export_journal(path: Path, state: dict, core=None) -> None:
    journal = read_json(path)
    # Legacy top-level records remain intact. The state ledger is authoritative
    # for v12; re-export after a crash is idempotent and never duplicates closes.
    journal["full_v12"] = {"version": VERSION, "instrument": state["instrument"],
                      "updated_at": iso(state["last_run_ts"]), "execution": "PAPER_SIGNAL",
                      "trades": state["trades"], "limit_orders": state["orders"],
                      "signals": state["signals"], "events": state["events"],
                      "pending": state.get("pending"), "active": state.get("active"),
                      "reconciliation": state.get("reconciliation"),
                      "second_slot": state.get('second_slot'), "pending_orders":state.get("pending_orders",[]),
                      "open_plans": open_plans(state),
                      "occupied_slots": occupied_slots(state), "max_slots": 2,
                      "max_pending": 1 if state.get('pending_policy') == PENDING_POLICY else 2,
                      "ledger_revision": 2, "order_sequence": state.get('order_sequence',0),
                      "setup_matrix": state.get("setup_matrix",[]), "watch":state.get("watch",[]),
                      "best_scenario":state.get("best_scenario"), "include_commission":state.get("include_commission",False),
                      "price_tick":state.get("price_tick"),
                      "reconciliation_history": state.get('reconciliation_history',[]),
                      "quality_summary": {"strategy":quality.STRATEGY,
                                          "observed_outcomes":len(state.get('quality',{}).get('observations',[])),
                                          "shadow_plans":len(state.get('quality',{}).get('pending',[])),
                                          "target_win_rate":quality.Settings().target_win_rate,
                                          "target_validated":False},
                      "statistics": statistics(state["trades"]),
                      "statistics_scope": "ALL_ENTRY_VERSIONS",
                      "dependency_statistics":trade_dependencies.summary(state['trades'],state.get('price_tick',.001)),
                      "statistics_by_entry_version": statistics_by_entry_version(state["trades"]),
                      "current_strategy_statistics": statistics_by_entry_version(state["trades"]).get(VERSION, statistics([])),
                      "setup_statistics": state.get("setup_statistics",{})}
    # Preserve complete legacy history and unknown fields. New ledger is merged by
    # stable identity; state remains authoritative if journal export fails once.
    for target,source,key in (("trades","trades","id"),("limit_orders","orders","order_id"),
                              ("signals","signals","id"),("signal_events","events","id")):
        existing = [r for r in journal.get(target,[]) if isinstance(r,dict)]
        positions = {str(r.get(key)): i for i,r in enumerate(existing) if r.get(key)}
        for row in state.get(source,[]):
            row = copy.deepcopy(row)
            if not row.get(key):
                row[key] = ident(source,row.get("ts"),row.get("action"),row.get("id"))
            if str(row[key]) in positions:
                existing[positions[str(row[key])]] = row
            else:
                positions[str(row[key])] = len(existing)
                existing.append(row)
        journal[target] = existing
    journal["version"] = VERSION
    journal["architecture_version"] = SCHEMA
    journal["updated_at"] = iso(state["last_run_ts"])
    if core is not None:
        for key, calculate in (("analytics",core.compute_analytics),
                               ("entry_quality_audit",core.compute_entry_quality_audit),
                               ("calendar_statistics",core.compute_calendar_statistics),
                               ("learning_status",core.compute_learning_status),
                               ("execution_model_statistics",core.compute_execution_model_statistics),
                               ("setup_statistics",core.compute_setup_statistics)):
            journal[key] = calculate(journal)
    atomic_write(path, journal)


def http_json(url: str, payload: Optional[dict] = None, timeout: int = 12) -> dict:
    body = json.dumps(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json",
                                                             "User-Agent": "ICT-Limit-Signal/12"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response)






def notify_queue(state: dict, token: str, chat_id: str, path: Path, persist=None) -> bool:
    if not token or not chat_id:
        raise ValueError("Telegram token/chat_id required for --notify")
    while state["notification_queue"]:
        item = state["notification_queue"][0]
        import notification_report
        body=notification_report.clean_queued_message(item['text'])
        if not body.strip():
            # Obsolete queued pages consisting solely of old version-wide
            # statistics must not block the delivery of later close events.
            state['notification_queue'].pop(0)
            persist() if persist else atomic_write(path, state)
            continue
        if not item.get('version') and ('Найближчі сетапи:' in body or 'після комісій' in body):
            body='🕓 Збережений звіт старого формату з черги\n'+body
        body=decorate_message(body)
        try:
            data = http_json("https://api.telegram.org/bot"+token+"/sendMessage",
                             {"chat_id": chat_id, "text": body})
            if not data.get("ok"):
                description=str(data.get('description','Unknown Telegram rejection'))
                print('::error::Telegram rejected message: '+description.replace(token,'[REDACTED]')[:250], file=sys.stderr)
                return False
        except urllib.error.HTTPError as exc:
            # urllib HTTPError contains a URL with the bot token; never log the URL.
            description=''
            try:
                response=json.loads(exc.read().decode('utf-8',errors='replace'))
                description=str(response.get('description',''))
            except (ValueError,TypeError,OSError):
                pass
            safe=description.replace(token,'[REDACTED]')[:200] if description else 'Check bot/chat permissions'
            print(f'::error::Telegram API HTTP {exc.code}: {safe}; message queued for retry',file=sys.stderr)
            return False
        except Exception:
            # Do not print exceptions; their URL could embed the secret token.
            print('::error::Telegram network error: queued for retry; check outbound access',file=sys.stderr)
            return False
        state["notification_queue"].pop(0)
        persist() if persist else atomic_write(path, state)
        print('[TELEGRAM] Message delivered successfully; queued pages remaining: '
              +str(len(state['notification_queue'])),flush=True)
    return True


def replay(data: dict, cfg: Config) -> dict:
    """15m causal replay. Warmup precedes optional start_ts; no future HTF bars."""
    state = new_state(cfg)
    replay_steps={**CANDLE_TF,'5m':300_000}
    raw = {tf: sorted([Candle.parse(r) for r in data["candles"].get(tf, [])], key=lambda c:c.ts)
           for tf in replay_steps}
    raw = {tf: sorted({c.ts: c for c in rows if c.confirmed}.values(), key=lambda c:c.ts)
           for tf, rows in raw.items()}
    smt_rows = [Candle.parse(r) for r in data.get("smt_candles_15m",[])]
    smt_rows = sorted({c.ts:c for c in smt_rows if c.confirmed}.values(),key=lambda c:c.ts)
    smt_available = [c.ts+TF["15m"] for c in smt_rows]
    available = {tf: [c.ts+replay_steps[tf] for c in rows] for tf, rows in raw.items()}
    if not raw["3m"]:
        raise ValueError("Replay needs 3m OHLC history")
    times = sorted({((c.ts+TF["3m"]+TF["15m"]-1)//TF["15m"])*TF["15m"]
                    for c in raw["3m"]})
    start = int(data.get("start_ts", times[0]))
    end = int(data.get("end_ts", times[-1]))
    actions = {}
    for now in times:
        if not start <= now <= end:
            continue
        current = {}
        for tf, rows in raw.items():
            index = bisect_right(available[tf], now)
            # Match the live 300-candle window; binary search excludes future data.
            current[tf] = rows[max(0, index-300):index]
        if not current["3m"]:
            continue
        price = current["3m"][-1].close
        spread = max(cfg.tick, price*finite(data.get("spread_bps", 2))/10_000)
        s = Snapshot(now, price, current, price-spread/2, price+spread/2, True, now, cfg.instrument)
        smt_index = bisect_right(smt_available,now)
        s.smt_candles = smt_rows[max(0,smt_index-300):smt_index]
        out = run_cycle(state, s, cfg)
        actions[out["action"]] = actions.get(out["action"], 0)+1
    return {"version": VERSION, "execution": "PAPER_REPLAY", "config": config_dict(cfg),
            "statistics": statistics(state["trades"]), "actions": actions,
            "trades": state["trades"], "orders": state["orders"], "events": state["events"],
            "active_at_end": state["active"], "pending_at_end": state["pending"],
            "open_plans_at_end": open_plans(state), "second_slot_at_end": state['second_slot'],
            "quality": state.get('quality',{}),
            "limitations": ["OHLC execution proxy; no queue position or actual exchange fills",
                            "Stop wins same-bar ambiguity; entry-bar TP needs a close beyond TP",
                            "Stop adjustments only at 15m reports, effective next whole 3m bar",
                            "No funding fees or size-dependent market impact",
                            "Unclosed plans excluded from win rate; inspect active_at_end",
                            "Use an untouched date range for out-of-sample evaluation"]}


def timestamp(value: Any, fallback: int = 0) -> int:
    if isinstance(value,(int,float)):
        return int(value)
    if value:
        try:
            return int(datetime.fromisoformat(str(value).replace('Z','+00:00')).timestamp()*1000)
        except ValueError:
            raise ValueError("Invalid legacy timestamp; state was not reset")
    return fallback


def convert_legacy_plan(raw: dict, cfg: Config, active: bool = False) -> dict:
    plan = dict(raw.get("plan") or raw)
    candidate = dict(raw.get("candidate") or {})
    side_raw = raw.get("side",plan.get("side"))
    side = 1 if str(side_raw).upper() in {'LONG','1'} else -1 if str(side_raw).upper() in {'SHORT','-1'} else 0
    if not side:
        raise ValueError("Legacy order has no valid direction; refusing silent migration")
    entry = finite(raw.get("entry",raw.get("limit_price",plan.get("entry",0))))
    initial = finite(raw.get("stop_initial",raw.get("initial_stop",plan.get("stop",0))))
    stop = finite(raw.get("stop_current",raw.get("stop",plan.get("stop",0))))
    risk = side*(entry-initial)
    tp1 = finite(raw.get("tp1",plan.get("tp1",plan.get("tp",0))))
    if min(entry,initial,stop,tp1)<=0 or risk<=0 or side*(tp1-entry)<=0:
        raise ValueError("Legacy plan geometry invalid; refusing silent migration")
    oid = str(raw.get("order_id",raw.get("id",ident(entry,initial,raw.get("opened_at")))))
    setup = str(raw.get("setup_type",raw.get("setup",candidate.get("setup_type","UNKNOWN"))))
    placed = timestamp(raw.get("placed_ts",raw.get("opened_at",0)))
    expiry = timestamp(raw.get("expires_ts"),placed+cfg.ttl_minutes*60_000)
    p = {"id": oid,"order_id": oid,"event_key": ident('legacy',oid),"zone_key": ident('legacy',oid),
         "instrument": cfg.instrument,"side": side,"setup": setup,"setup_type": setup,
         "setup_family": raw.get("setup_family",cfg.core.journal_setup_family(setup)),
         "canonical_setup_family": cfg.core.canonical_setup_family(setup),"timeframe": "LEGACY",
         "entry": entry,"limit_price": entry,"stop": stop,"initial_stop": initial,"risk": risk,
         "tp": tp1,"tp0": finite(raw.get("tp0",plan.get("tp0",entry+side*.6*risk))),
         "tp1": tp1,"tp2": finite(raw.get("tp2",plan.get("tp2",tp1))),
         "tp3": finite(raw.get("tp3",plan.get("tp3",tp1))),
         "partials": dict(plan.get("partial_plan") or raw.get("partials") or
                           {"TP1": raw.get("tp1_size_pct",.65),"TP2":raw.get("tp2_size_pct",.20),"TP3":raw.get("tp3_runner_pct",.15)}),
         "score": raw.get("quality",raw.get("score",60)),"net_rr": side*(tp1-entry)/risk,
         "stop_distance_pct": risk/entry*100,"placed_ts": placed,"expires_ts": expiry,
         "zone_created_ts": placed,"invalidation": raw.get("structural_invalidation",initial),
         "evidence": ["Existing legacy plan preserved; geometry not rewritten"],"parent_keys": [],
         "status": "OPEN" if active else "PENDING","last_bar": 0,
         "entry_version": raw.get("bot_version_at_entry",raw.get("version","LEGACY")),
         "execution_source": raw.get("execution_source","LIMIT_ARMED_AT_LEVEL"),
         "execution": "PAPER_SIGNAL","migration_contract": "PRESERVE_EXISTING_LEVELS"}
    # Original partial plan can use uppercase labels; validate and preserve it.
    if not all(0<=finite(p['partials'].get(k,0))<=1 for k in ('TP1','TP2','TP3')):
        raise ValueError("Invalid legacy partial fractions")
    if sum(p['partials'].get(k,0) for k in ('TP1','TP2','TP3'))>1.00001:
        raise ValueError("Legacy partial fractions exceed position")
    if active:
        filled = timestamp(raw.get("filled_ts",raw.get("opened_at",placed)))
        last = int(raw.get("last_checked_3m_ts",raw.get("last_bar",0)) or filled-TF['3m'])
        p.update(filled_ts=filled,opened_at=iso(filled),last_bar=last,
                 exit_deadline=min(filled+cfg.max_hold_minutes*60_000,session_cutoff(filled,cfg)),
                 mfe_r=raw.get("mfe_r",0),mae_r=raw.get("mae_r",0),strong_closes=0,
                 remaining=1.0,realized_gross_r=0.0,realized_exit_fees_r=0.0,realized_legs=[])
        for name in ('TP1','TP2'):
            if raw.get(name.lower()+'_hit'):
                fraction = p['partials'].get(name,0)
                level = p[name.lower()]
                p[name.lower()+'_hit'] = True
                p['remaining'] -= fraction
                p['realized_gross_r'] += fraction*side*(level-entry)/risk
                p['realized_exit_fees_r'] += fraction*level*fee_rate(cfg,'taker')/risk
        if 'LIMIT' not in p['execution_source']:
            p['entry_fee_rate'] = fee_rate(cfg,'taker')
    return p


def migrate_state(raw: dict, cfg: Config) -> dict:
    if raw.get('schema') == 'ict_limit_state_v11':
        out = new_state(cfg)
        out['legacy_snapshot'] = copy.deepcopy(raw)
        out['instrument'] = raw.get('instrument',cfg.instrument)
        out['last_run_ts'] = int(raw.get('last_run_ts',0))
        out['reconciliation'] = copy.deepcopy(raw.get('reconciliation'))
        out['notification_queue'] = copy.deepcopy(raw.get('notification_queue',[]))
        for slot in ('pending','active'):
            p = copy.deepcopy(raw.get(slot))
            if p:
                side = p.get('side',0)
                if side not in (-1,1) or p.get('risk',0)<=0 or min(p.get('entry',0),p.get('stop',0),p.get('tp',0))<=0:
                    raise ValueError('Invalid v11 open plan; migration did not reset it')
                p['entry_version'] = raw.get('version','ict-limit-v11.0.0')
                p['migration_contract'] = 'PRESERVE_EXISTING_LEVELS'
                p['level_key'] = ident('LONG' if side==1 else 'SHORT',round(p['entry']/cfg.tick))
                p.setdefault('setup_type',p.get('setup','UNKNOWN'))
                p.setdefault('order_id',p['id'])
            out[slot] = p
        out['migration'] = {'source_version':raw.get('version'),'old_state_preserved':True,
                            'active_preserved':bool(out['active']),'pending_preserved':bool(out['pending'])}
        return out
    known = raw.get('architecture_version','').startswith('ORGANIC_') or 'active_trade' in raw or 'anchors_v10' in raw
    if not known:
        raise ValueError("Unknown state architecture. Preserve it and use a separate state path")
    out = new_state(cfg)
    out['legacy_snapshot'] = copy.deepcopy(raw)
    active = raw.get('active_trade')
    if active and str(active.get('status','OPEN')).upper()=='OPEN':
        out['active'] = convert_legacy_plan(active,cfg,True)
        out['last_run_ts'] = out['active']['last_bar']+TF['3m']
    pending = raw.get('pending_limit_v10') or []
    if isinstance(pending,dict):
        pending = [pending]
    pending = [p for p in pending if p.get('status','PENDING')=='PENDING']
    if len(pending)==1 and not out['active']:
        out['pending'] = convert_legacy_plan(pending[0],cfg)
        out['last_run_ts'] = out['pending']['placed_ts']
    elif pending:
        out['legacy_pending_orders'] = copy.deepcopy(pending)
        out['reconciliation'] = {'order_id': ','.join(str(p.get('order_id')) for p in pending),
                                 'reason':'LEGACY_MULTIPLE_OR_ACTIVE_PLUS_PENDING','ts':out['last_run_ts']}
    for raw_anchor in raw.get('anchors_v10',[]):
        a = cfg.core.anchor_from_dict(raw_anchor)
        if a:
            out['anchor_memory'][anchor_key(a,cfg)] = cfg.core.anchor_to_dict(a)
    out['migration'] = {'source_version': raw.get('version'), 'active_preserved':bool(out['active']),
                        'pending_preserved':len(pending),'old_state_preserved':True}
    return out


def resolve_live_config(cfg: Config) -> Config:
    """Use OKX's current price grid before initializing or migrating state.

    PRICE_TICK_SIZE remains an offline/replay setting. Live exchange metadata
    is authoritative; an outdated workflow variable must not stop the bot.
    Existing open-plan geometry is still protected by the config-hash guard.
    """
    core = cfg.core
    url = 'https://www.okx.com/api/v5/public/instruments?'+urllib.parse.urlencode(
        {'instType':'SWAP' if cfg.instrument.endswith('-SWAP') else 'SPOT','instId':cfg.instrument})
    response = core.http_get(url)
    body = response.json() if response else {}
    if str(body.get('code')) != '0':
        raise ValueError('Unable to verify instrument metadata from OKX')
    record = next((r for r in body.get('data',[]) if r.get('instId')==cfg.instrument),None)
    if record is None or record.get('state')!='live':
        raise ValueError('Instrument unavailable or not live')
    tick = finite(record.get('tickSz',0))
    if tick <= 0:
        raise ValueError('OKX returned an invalid price tick size')
    effective = replace(cfg,tick=tick)
    effective.validate()
    if abs(tick-cfg.tick)>max(tick,cfg.tick)*1e-8:
        print(f'[INFO] OKX price tick: {tick:g}; live calculations use the exchange price grid')
    return effective


def collect_snapshot(cfg: Config, checkpoint: int = 0) -> Snapshot:
    core = cfg.core
    data = core.collect_market_data()
    data.setdefault('candles',{})
    for tf in ('1m','3m'):
        raw = data['candles'].get(tf,[])
        if checkpoint and raw:
            for _ in range(24):
                earliest=min(c.ts for c in raw)
                if earliest<=checkpoint: break
                url=f"{core.OKX_BASE_URL}/history-candles?"+urllib.parse.urlencode(
                    {'instId':cfg.instrument,'bar':tf,'after':earliest,'limit':300})
                resp=core.http_get(url);body=resp.json() if resp else {}
                older=[Candle.parse(row) for row in body.get('data',[])] if body.get('code')=='0' else []
                if not older or min(c.ts for c in older)>=earliest: break
                raw.extend(older)
        data['candles'][tf]=raw
    current=[c for c in data['candles'].get('1m',[]) if not c.confirmed]
    if current:
        data['current_candle_1m']=asdict(max(current,key=lambda c:c.ts))
    data['require_1m']=True
    data['now'] = int(time.time()*1000)
    data['trusted'] = data.get('execution_price_trusted',False)
    data['candles'] = {tf:[asdict(c) for c in rows] for tf,rows in data.get('candles',{}).items()}
    data['smt_candles_15m'] = [asdict(c) for c in data.get('smt_candles_15m',[])]
    if (data.get('price_source')=='OKX_TICKER' and data.get('execution_venue')=='OKX'
            and data.get('trusted') is True and data.get('instrument')==cfg.instrument):
        data['research_provenance']={'origin':research_evidence.ORIGIN,
                                    'instrument':cfg.instrument,'observed_ts':data['now']}
    return Snapshot.parse(data)


def report_due(now_ms: int, last_report_ms: int) -> bool:
    """A unique notification per UTC 15m bucket despite late GitHub starts.

    Persisted report timestamp is updated in the same ledger transaction. The
    first run after deployment reports immediately. Scan/report cadence is
    independent; a failed Telegram delivery stays queued for later delivery.
    """
    return now_ms // TF['15m'] > max(0, last_report_ms) // TF['15m']


def notification_checkpoint(state, path):
    # Keep all position, order, PnL, cancellation and notification identity.
    # Bulky detector diagnostics can be rebuilt; they delay live delivery.
    skip={'anchor_memory','signals','watch','setup_matrix','quality_archives',
          'setup_statistics','best_scenario','setup_renewals','stale_refresh'}
    atomic_write(path,{k:v for k,v in state.items() if k not in skip})


def run_live(core, snapshot_path: Optional[Path] = None, notify: bool = False,
             ack: bool = False, auto_report: bool = False) -> int:
    if core.BOT_VERSION != VERSION:
        raise ValueError('Неповне оновлення: оновіть bot_oneshot.py і full_limit_engine.py з одного архіву')
    expected=os.environ.get('EXPECTED_BOT_VERSION')
    if expected and expected != VERSION:
        raise ValueError('Workflow очікує іншу версію коду: оновіть весь ZIP')
    cfg = replace(Config.from_env(),core=core)
    state_path,journal_path = Path(core.STATE_FILE),Path(core.JOURNAL_FILE)
    if state_path.resolve()==journal_path.resolve():
        raise ValueError('State and journal paths must differ')
    if (notify or auto_report) and (not core.TELEGRAM_TOKEN or not core.TELEGRAM_CHAT_ID):
        raise ValueError('Telegram credentials required before starting a reporting cycle')
    timing_enabled = os.environ.get('BOT_STAGE_TIMING', '').strip().lower() in ('1', 'true', 'yes')
    stage_start = time.monotonic()
    def stage(name: str) -> None:
        nonlocal stage_start
        now = time.monotonic()
        if timing_enabled:
            print(f'[PERF] {name}: {now-stage_start:.2f}s', flush=True)
        stage_start = now
    with locked(Path(str(state_path)+'.lock')):
        if snapshot_path is None:
            cfg = resolve_live_config(cfg)
        state = load_state(state_path,cfg)
        checkpoint_path=state_path.with_name('notification_checkpoint_v12.json')
        recovery=read_json(checkpoint_path)
        if recovery and recovery.get('last_run_ts',0)>=state.get('last_run_ts',0):
            if recovery.get('instrument')!=cfg.instrument:
                raise ValueError('Notification checkpoint belongs to a different instrument')
            state.update(recovery)
        if auto_report:
            notify = report_due(int(time.time()*1000),int(state.get('last_report_requested_ts',0) or 0))
        try:
            quality.seed(state,cfg,Path(os.environ.get('QUALITY_MODEL_FILE',str(ROOT/'quality_model.json'))))
            state.pop('quality_model_warning',None)
        except (ValueError,OSError,KeyError,TypeError) as exc:
            # An optional research artifact must never stop open-trade care.
            # Its incompatible evidence is not used; existing matching evidence
            # and the closed-bar confirmation policy remain available.
            state['quality_model_warning']={'reason':str(exc),'ts':int(time.time()*1000)}
            print('QUALITY_MODEL_REJECTED: '+str(exc)+'; continuing lifecycle',file=sys.stderr)
        read_json(journal_path)
        if ack and state.get('reconciliation'):
            state.setdefault('reconciliation_history',[]).append({**copy.deepcopy(state['reconciliation']),
                                                                 'acknowledged_ts':int(time.time()*1000)})
            emit(state,'RECONCILIATION_ACKNOWLEDGED',int(time.time()*1000),order_id=state['reconciliation']['order_id'])
            state['reconciliation'] = None
            state['legacy_pending_orders'] = []
        if ack and state.get('second_slot',{}).get('reconciliation'):
            r = state['second_slot']['reconciliation']
            state.setdefault('reconciliation_history',[]).append({**copy.deepcopy(r),'acknowledged_ts':int(time.time()*1000)})
            emit(state,'RECONCILIATION_ACKNOWLEDGED',int(time.time()*1000),order_id=r['order_id'])
            state['second_slot']['reconciliation'] = None
        stage('init_state_and_model')
        snap = Snapshot.parse(read_json(snapshot_path)) if snapshot_path else collect_snapshot(cfg,state['last_run_ts'])
        stage('market_snapshot')
        old_events = len(state.get('events',[]))
        # Every invocation scans closed data. Only auto_report/notify builds
        # a Telegram report; on an always-on host this is every third 3m bar.
        event_watermark = min(state.get('last_notified_event_count',old_events),old_events)
        result = run_cycle(state,snap,cfg)
        stage('strategy_and_lifecycle')
        if notify:
            result['events'] = sorted(state['events'][event_watermark:],key=lambda e:e['ts'])
        stage('research_archive')
        message = build_message(state,snap,result,cfg) if notify else ''
        stage('compose_report')
        state.setdefault('last_notified_event_count',old_events)
        if notify and result['action']!='DUPLICATE_OR_OUT_OF_ORDER':
            state['last_notified_event_count']=len(state['events'])
        if message:
            print(message)
        # A report is a notification event, not a trading event. A delayed or
        # duplicate market snapshot must not silently skip a requested report.
        if notify:
            if not message.strip():
                print('::error::Telegram report is empty; refusing silent success',file=sys.stderr)
                return 2
            import notification_report
            report_ts=int(time.time()*1000)
            for page, part in enumerate(notification_report.split_message(message)):
                state['notification_queue'].append({'id':ident(report_ts,page,part),'text':part,
                                                    'version':VERSION,'ts':report_ts})
            state['last_report_requested_ts']=report_ts
            print('[TELEGRAM] 15m report queued; pages: '
                  +str(len(state['notification_queue'])),flush=True)
        delivery_ok=True
        if state['notification_queue']:
            notification_checkpoint(state,checkpoint_path)
            if not core.TELEGRAM_TOKEN or not core.TELEGRAM_CHAT_ID:
                delivery_ok=False
            else:
                delivery_ok=notify_queue(state,core.TELEGRAM_TOKEN,core.TELEGRAM_CHAT_ID,
                    checkpoint_path,persist=lambda:notification_checkpoint(state,checkpoint_path))
        stage('telegram_delivery')
        archive_path=Path(os.environ.get('RESEARCH_ARCHIVE_FILE',str(state_path.with_name('research_market_v12_6.json'))))
        try:
            # Offline/test snapshots never acquire live provenance just because
            # a caller requested --snapshot or patched collect_snapshot.
            if snapshot_path is not None:
                snap.metadata.pop('research_provenance',None)
            state['market_history_status']=research_evidence.record(archive_path,snap,cfg,sys.modules[__name__])
        except (ValueError,OSError) as exc:
            state['market_history_status']={'status':'FAILED','error':str(exc)}
            print('Market evidence recording failed: '+str(exc),file=sys.stderr)
        stage('research_archive')
        atomic_write(state_path,state)
        stage('save_state')
        export_journal(journal_path,state,core)
        stage('save_journal')
        live_path = os.environ.get('LIVE_STATE_FILE')
        if live_path and snapshot_path is None and result['action'] != 'DUPLICATE_OR_OUT_OF_ORDER':
            import live_terminal
            atomic_write(Path(live_path), live_terminal.build(state,snap,result,cfg,sys.modules[__name__]))

        if checkpoint_path.exists():
            checkpoint_path.unlink()
        if not delivery_ok:
            print('::error::Telegram delivery failed; report retained for retry',file=sys.stderr)
            return 2
        if result['action']=='DATA_HOLD':
            print('DATA_HOLD: no new orders; Telegram summary/state were processed',file=sys.stderr)
        return 0  # preserve queued notifications/ledger even when data are on hold


def main(core) -> int:
    parser = argparse.ArgumentParser(description='Full 24-setup ICT LIMIT bot v12')
    parser.add_argument('--self-test',action='store_true')
    parser.add_argument('--all-archived-tests',action='store_true',
                        help='With --self-test, also run historical tests for superseded policies')
    parser.add_argument('--legacy-self-test',action='store_true',help='Original compatibility checks; no production execution')
    parser.add_argument('--audit-journal',type=Path)
    parser.add_argument('--replay',type=Path)
    parser.add_argument('--snapshot',type=Path)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--notify',action='store_true')
    parser.add_argument('--auto-report',action='store_true',help='Send no more than one Telegram report per 15m bucket')
    parser.add_argument('--ack-unresolved',action='store_true')
    args = parser.parse_args()
    if args.legacy_self_test:
        # Frozen legacy fixtures were designed around the original 0.4% core
        # screening heuristic. Override it ONLY inside this offline compatibility
        # harness; checklist entries use the configured absolute structural stop bounds.
        if not hasattr(core, "ABS_MIN_STOP_PCT"):
            return 0 if core._run_self_test() else 1
        saved_core_floor = core.ABS_MIN_STOP_PCT
        try:
            core.ABS_MIN_STOP_PCT = 0.004
            return 0 if core._run_self_test() else 1
        finally:
            core.ABS_MIN_STOP_PCT = saved_core_floor
    cfg = replace(Config.from_env(),core=core)
    if args.self_test:
        import unittest
        from unittest.mock import patch
        # Active release contracts. Superseded entry/exit policies remain
        # available explicitly through --all-archived-tests.
        modules=('test_full_limit_engine.DataTests','test_v12_20_3_telegram_delivery',
                 'test_v12_18_speed','test_v12_23_master.MicroBrake','test_v12_23_master.Vwap',
                 'test_v12_22_precision.Precision.test_all_24_precision_contracts_reject_empty_trigger',
                 'test_v12_25_live_terminal', 'test_v12_26_pro_cockpit', 'test_v12_27_integrity',
                 'test_v12_28_quant', 'test_v12_29_quant', 'test_v12_29_1_checklist', 'test_v12_30_institutional', 'test_v12_31_market_wave')
        suite=(unittest.defaultTestLoader.discover(str(ROOT),pattern='test_*.py')
               if args.all_archived_tests else unittest.defaultTestLoader.loadTestsFromNames(modules))
        if not suite.countTestCases():
            raise ValueError('test_full_limit_engine.py required')
        # GitHub's job-level MARKET_HISTORY_FILE also reaches tests that mock
        # run_live. Never let fixtures append artificial prices to live evidence.
        previous_archive = os.environ.get('MARKET_HISTORY_FILE')
        previous_research = os.environ.get('RESEARCH_ARCHIVE_FILE')
        previous_entry_mode = os.environ.pop('BZU_ENTRY_MODE', None)
        with tempfile.TemporaryDirectory(prefix='bzu-self-test-') as directory:
            os.environ['MARKET_HISTORY_FILE'] = str(Path(directory)/'market.json')
            os.environ['RESEARCH_ARCHIVE_FILE'] = str(Path(directory)/'research.json')
            try:
                runner = unittest.TextTestRunner(verbosity=2)
                if args.all_archived_tests:
                    return 0 if runner.run(suite).wasSuccessful() else 1
                # Older exact-price fixtures verify the legacy geometry and
                # ledger contracts. The Brent suite exercises the live v12.32 policy.
                with patch.object(oil_risk, 'enabled', return_value=False):
                    compatibility = runner.run(suite)
                organic = runner.run(unittest.defaultTestLoader.loadTestsFromName('test_v12_32_organic_oil'))
                return 0 if compatibility.wasSuccessful() and organic.wasSuccessful() else 1
            finally:
                if previous_entry_mode is not None:
                    os.environ['BZU_ENTRY_MODE'] = previous_entry_mode
                else:
                    os.environ.pop('BZU_ENTRY_MODE', None)
                if previous_archive is None:
                    os.environ.pop('MARKET_HISTORY_FILE',None)
                else:
                    os.environ['MARKET_HISTORY_FILE'] = previous_archive
                if previous_research is None:
                    os.environ.pop('RESEARCH_ARCHIVE_FILE',None)
                else:
                    os.environ['RESEARCH_ARCHIVE_FILE'] = previous_research
    if args.audit_journal or args.replay:
        if args.audit_journal:
            with market_clock(core,int(time.time()*1000)):
                report = core.run_audit_journal(str(args.audit_journal))
            report['full_v12'] = read_json(args.audit_journal).get('full_v12',{})
        else:
            report = replay(read_json(args.replay),cfg)
        if args.output:
            atomic_write(args.output,report)
        print(json.dumps(report,ensure_ascii=False,indent=2,allow_nan=False))
        return 0
    notify = args.notify or (bool(core.TELEGRAM_TOKEN and core.TELEGRAM_CHAT_ID)
                            and core.TELEGRAM_NOTIFY_EVERY_RUN and not args.snapshot and not args.auto_report)
    return run_live(core,args.snapshot,notify,args.ack_unresolved,auto_report=args.auto_report)
