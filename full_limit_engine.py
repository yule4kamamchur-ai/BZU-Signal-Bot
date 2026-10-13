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
from bisect import bisect_right
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, replace, fields
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from statistics import mean
from typing import Any, Optional
from zoneinfo import ZoneInfo

VERSION = "full-ict-v12.4.3-minimum-sl-tp"
REPORT_RELEASE = 'v12.4.3'
import signal_quality as quality
import setup_scanner as scanner
import price_forecast
SCHEMA = "full_ict_limit_state_v12"
TF = {"3m": 180_000, "15m": 900_000, "1H": 3_600_000, "4H": 14_400_000}
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
    min_net_rr: float = 1.20
    max_target_r: float = 2.0
    max_stop_atr: float = 2.0
    min_stop_atr3: float = 0.65
    min_stop_pct: float = 0.01  # 1% of ENTRY for every new limit
    min_target_pct: float = 0.015  # TP1 at least 1.5% from ENTRY
    max_entry_distance_atr: float = 3.0
    min_score: float = 60.0
    conflict_margin: float = 8.0
    ttl_minutes: int = 120
    setup_age_minutes: int = 180
    max_hold_minutes: int = 360
    day_loss_cap_r: float = 3.0
    max_daily_trades: int = 5
    timezone: str = "Europe/Kyiv"
    session_end_hour: int = 23
    break_even_trigger_r: float = 1.0
    max_spread_atr: float = 0.15
    cancel_on_bias_flip: bool = True
    core: Any = field(default=None, repr=False, compare=False)
    setup_min_sample: int = 20
    partial_tp1: float = 0.65
    partial_tp2: float = 0.20

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
        if not 0 <= self.min_score <= 100 or self.max_daily_trades < 1:
            raise ValueError("Invalid ranking / daily trade limit")
        if not 0.01 <= self.min_stop_pct < 0.10 or self.min_stop_atr3 <= 0 or self.max_stop_atr <= 0:
            raise ValueError("Invalid stop bounds")
        if not 0.015 <= self.min_target_pct < 0.50:
            raise ValueError("TP1 must be at least 1.5% from entry")
        if self.partial_tp1+self.partial_tp2 >= 1 or self.setup_min_sample < 10:
            raise ValueError("Invalid partial plan / calibration sample")
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
            "min_stop_pct": ("ABS_MIN_STOP_PCT", float),
            "min_target_pct": ("MIN_TP1_DISTANCE_PCT", float),
            "day_loss_cap_r": ("DAILY_LOSS_CAP_R", float),
            "max_daily_trades": ("MAX_DAILY_TRADES", int),
            "timezone": ("TRADING_TIMEZONE", str),
            "session_end_hour": ("SESSION_END_HOUR", int),
            "min_score": ("MIN_SETUP_SCORE", float),
            "max_entry_distance_atr": ("LIMIT_MAX_DISTANCE_ATR", float),
            "partial_tp1": ("TP1_SIZE_PCT", float), "partial_tp2": ("TP2_SIZE_PCT", float),
        }
        kwargs = {k: cast(os.environ[name]) for k, (name, cast) in mapping.items()
                  if os.environ.get(name)}
        flag=os.environ.get('INCLUDE_COMMISSION','false').lower()
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
        for tf, step in TF.items():
            rows = data.get("candles", {}).get(tf, [])
            parsed = [Candle.parse(r) for r in rows]
            # A candle's timestamp is its OPEN time, not its closing time.
            candles[tf] = sorted({c.ts: c for c in parsed if c.confirmed and c.ts + step <= now}.values(),
                                 key=lambda c: c.ts)
        trust = data.get("trusted", False)
        obj = cls(now, finite(data["price"]), candles, finite(data.get("bid", 0)),
                   finite(data.get("ask", 0)), trust is True,
                   int(data.get("ticker_ts", now)), str(data.get("instrument", "")))
        smt = [Candle.parse(r) for r in data.get("smt_candles_15m", [])]
        obj.smt_candles = sorted({c.ts:c for c in smt if c.confirmed and c.ts+TF["15m"]<=now}.values(),
                                key=lambda c:c.ts)
        obj.metadata = {k: v for k, v in data.items() if k not in ("candles", "smt_candles_15m")}
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
    for tf, step in TF.items():
        rows = s.candles.get(tf, [])
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
            errors.append("SPREAD_TOO_WIDE")
    return errors










def fees_r(entry: float, exit_price: float, risk: float, cfg: Config) -> float:
    # Take-profit conservatively assumes taker fees too. Rates must match account.
    return (entry*fee_rate(cfg,'maker') + exit_price*fee_rate(cfg,'taker'))/risk


def fee_rate(cfg: Config, kind: str) -> float:
    return getattr(cfg,kind+'_fee') if cfg.include_commission else 0.


REVERSALS = frozenset({"SWEEP_RECLAIM", "CAPITULATION_RECOVERY", "RANGE_EDGE_REVERSAL",
                      "FAILED_AUCTION_REJECTION", "LIQUIDITY_SWEEP_REVERSAL_SHORT",
                      "BUYER_EXHAUSTION_SHORT", "FAILED_BREAKOUT_SHORT", "MSS_REVERSAL_SHORT",
                      "FAILED_OPENING_RANGE_BREAKOUT", "OR_FAILURE_2_SHORT", "DIRECTION_FLIP_15M"})


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


def anchor_key(a, cfg: Config) -> str:
    tf = a.evidence.get('setup_timeframe', '15m')
    parts = (a.setup_type, a.side, round(a.level/cfg.tick), round(a.invalidation/cfg.tick), a.kind)
    return ident(*parts) if tf == '15m' else ident(*parts, tf)


def merge_full_anchors(fresh: list, state: dict, s: Snapshot, cfg: Config) -> list:
    """Immutable first observation; repeated prints cannot renew the same level."""
    core = cfg.core
    memory = state.setdefault("anchor_memory", {})
    for a in fresh:
        key = anchor_key(a, cfg)
        previous = memory.get(key)
        if previous:
            a.id = previous["id"]
            a.created_ts = previous["created_ts"]
            a.expires_ts = previous["expires_ts"]
            a.cooldown_until_ts = previous.get("cooldown_until_ts", 0)
        a.expires_ts = min(a.expires_ts, a.created_ts+cfg.setup_age_minutes*scanner.MINUTES[a.evidence.get("setup_timeframe","15m")]//15*60_000)
        memory[key] = core.anchor_to_dict(a)
    # Retain tombstones beyond setup lifetime to prevent a detector refreshing age.
    memory = {k: v for k, v in memory.items() if s.now-v["created_ts"] <= 7*86_400_000}
    state["anchor_memory"] = memory
    return [a for raw in memory.values() for a in [core.anchor_from_dict(raw)] if a is not None
            and a.created_ts <= s.now < a.expires_ts
            and a.state not in {"TRIGGERED", "INVALIDATED", "REJECTED"}]


def full_targets(ctx: dict, s: Snapshot, side: str, entry: float, cfg: Config) -> list[dict]:
    core = cfg.core
    sign = core.side_sign(side)
    # Evaluate the SAME entry price; ticker price cannot move a limit's runway.
    local = dict(ctx, price=entry)
    targets = core.technical_targets(local, side, entry)
    for tf in ("1H", "4H"):
        snap = core.structure_snapshot(s.candles[tf], 60, 2)
        key = "swing_highs" if sign == 1 else "swing_lows"
        for level in snap.get(key, []):
            if sign*(level-entry) > 0:
                targets.append({"kind": tf+"_LIQUIDITY", "level": level})
    for t in targets:
        t["distance"] = sign*(t["level"]-entry)
    return sorted({round(t["level"]/cfg.tick): t for t in targets if t["distance"] > 2*cfg.tick}.values(),
                  key=lambda t:t["distance"])


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
    """Never move a target through a known closer structural obstacle.

    Report the required level separately when actual runway is insufficient;
    no too-close target is presented as TP1 or used for its forecast.
    """
    required_distance=max(cfg.min_target_pct*entry,cfg.min_net_rr*risk)
    required_tp=round_tick(entry+side*required_distance,cfg.tick,up=side==1)
    geometry={'entry':entry,'stop':stop,'initial_stop':stop,'risk':risk,
              'required_rr':cfg.min_net_rr,'required_tp':required_tp,
              'min_target_pct':cfg.min_target_pct,'target_distance_met':False}
    if not targets:
        return geometry
    nearest=targets[0]
    target=round_tick(entry+side*min(nearest['distance']-buffer,cfg.max_target_r*risk),
                      cfg.tick,up=side==-1)
    distance=side*(target-entry)
    distance_met=distance+1e-9>=cfg.min_target_pct*entry
    geometry.update(available_tp1=target,gross_rr=distance/risk,
                    tp1_distance_pct=distance/entry*100,target_distance_met=distance_met,
                    target_source=nearest['kind'],obstacle_level=nearest['level'])
    if distance_met:
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


def full_plan(a, ctx: dict, s: Snapshot, cfg: Config, stats: dict, diagnostics=None) -> tuple[Optional[dict], str]:
    core = cfg.core
    diag=diagnostics if diagnostics is not None else {}
    diag['entry_checks']=scanner.entry_checks()
    def mark(key,passed):
        next(c for c in diag['entry_checks'] if c['key']==key)['met']=bool(passed)
    side = str(a.side)
    source_tf = a.evidence.get('setup_timeframe', '15m')
    scale = scanner.MINUTES[source_tf] // 15
    sign = core.side_sign(side)
    a15, a3 = ctx["atr15"], ctx["atr3"]
    if not sign or min(a15, a3) <= 0:
        return None, "INVALID_DIRECTION_OR_VOLATILITY"
    if a.cooldown_until_ts > s.now:
        return None, "ANCHOR_COOLDOWN"
    entry = round_tick(a.level, cfg.tick, up=sign == -1)
    diag.update(entry=entry,level=entry)
    quote_gate=scanner.passive_gate(entry,sign,s.bid,s.ask,cfg.tick)
    diag['quote_gate']=quote_gate
    mark('PASSIVE_LIMIT',quote_gate['passive'])
    if not quote_gate['passive']:
        return None, "LIMIT_WOULD_CROSS_SPREAD"
    distance = abs(s.price-entry)/a15
    if not 0 <= distance <= cfg.max_entry_distance_atr:
        return None, "LIMIT_UNREACHABLE"
    if sign*(entry-a.invalidation) <= 0:
        return None, "INVALID_STRUCTURAL_FALSIFIER"
    observed = [c for c in s.candles["3m"] if c.ts >= a.created_ts]
    if any(sign*(c.close-a.invalidation) <= 0 for c in observed) or sign*(s.price-a.invalidation) <= 0:
        return None, "ANCHOR_INVALIDATED"
    if any(c.low <= entry if sign == 1 else c.high >= entry for c in observed):
        return None, "LEVEL_ALREADY_VISITED_AFTER_OBSERVATION"
    mark('FRESH_LEVEL',s.now<a.expires_ts and s.now-a.created_ts<=cfg.setup_age_minutes*scale*60_000)
    entry,stop,risk,floor,buffer=stop_geometry(a,ctx,cfg)
    diag.update(stop=stop,risk=risk)
    mark('VALID_STOP',risk+1e-9>=floor and risk<=max(cfg.max_stop_atr*a15,1.25*floor))
    if risk+1e-9 < floor:
        return None, "STOP_INSIDE_NOISE"
    if risk > max(cfg.max_stop_atr*a15, 1.25*floor):
        return None, "STRUCTURAL_STOP_TOO_WIDE"
    b1 = core.structure_snapshot(s.candles["1H"], 60, 2)["direction"]
    b4 = core.structure_snapshot(s.candles["4H"], 60, 2)["direction"]
    b15, b3 = ctx["structure15"]["direction"], ctx["structure3m"]["direction"]
    opposite = "SHORT" if sign == 1 else "LONG"
    both_against = b1 == opposite and b4 == opposite
    reversal_confirmed = a.setup_type in REVERSALS and b15 == side and b3 == side
    mark('HTF_ALIGNMENT',not both_against or reversal_confirmed)
    if both_against and not reversal_confirmed:
        return None, "BOTH_HTF_AGAINST_WITHOUT_CONFIRMED_REVERSAL"
    # Mixed HTF is assessed per side; it is not a blanket rejection of all setups.
    support = 50 + 15*int(b1 == side)+15*int(b4 == side)-12*int(b1 == opposite)-8*int(b4 == opposite)
    support += 10*int(b15 == side)+5*int(b3 == side)
    smt = ctx.get("smt", {})
    support_key = "supports_long" if sign == 1 else "supports_short"
    against_key = "supports_short" if sign == 1 else "supports_long"
    smt_score = 50
    if smt.get("available"):
        smt_score += 25*int(bool(smt.get(support_key)))-25*int(bool(smt.get(against_key)))
    cvd = ctx.get("cvd", {})
    cvd_direction = str(cvd.get("direction", cvd.get("bias", "NEUTRAL"))).upper()
    cvd_score = 70 if cvd_direction == side else (30 if cvd_direction == opposite else 50)
    regime = str(ctx.get("regime"))
    continuation = core.canonical_setup_family(a.setup_type) in {"TREND_CONTINUATION", "STRUCTURAL_EXPANSION", "SESSION_EXPANSION"}
    regime_score = 75 if regime == "TREND" and continuation else (65 if regime in {"RANGE", "TRANSITION"} else 50)
    targets = full_targets(ctx, s, side, entry, cfg)
    diag.update(target_geometry(entry,stop,risk,buffer,sign,targets,cfg))
    if not targets:
        return None, "NO_STRUCTURAL_TARGET"
    nearest = targets[0]
    tp1 = diag['available_tp1']
    rr1 = sign*(tp1-entry)/risk
    costs = (fees_r(entry, tp1, risk, cfg)+tp1*cfg.slippage_bps/10_000/risk) if cfg.include_commission else 0.
    net_rr = rr1-costs
    diag['net_rr']=net_rr
    mark('TARGET_RR',diag['target_distance_met'] and rr1>0 and net_rr>=cfg.min_net_rr)
    location = max(0, 100-22*distance)+min(15, 5*net_rr)
    score = .35*a.score+.25*min(100,max(0,support))+.20*min(100,location)+.10*regime_score+.05*smt_score+.05*cvd_score
    prior = stats.get(a.setup_type, {})
    prior = prior.get('timeframes',{}).get(source_tf,prior)
    if prior.get("status") == "NEGATIVE_EXPECTANCY":
        score -= min(12, abs(prior["expectancy_r"])*10)
    elif prior.get("status") == "POSITIVE_EXPECTANCY":
        score += min(5, prior["expectancy_r"]*5)
    score = round(min(100,max(0,score)), 2)
    diag['score']=score
    mark('CONTEXT_SCORE',score>=cfg.min_score)
    if not diag['target_distance_met']:
        return None, "TARGET_DISTANCE_TOO_SMALL"
    if rr1 <= 0 or net_rr < cfg.min_net_rr:
        return None, "TARGET_RR_TOO_LOW" if not cfg.include_commission else "NEAREST_TARGET_TOO_CLOSE_AFTER_COSTS"
    if cfg.include_commission and costs > .25*rr1:
        return None, "COSTS_CONSUME_EDGE"
    # The remaining targets also respect real obstacles, never synthetic 5R promises.
    later = [t for t in targets if t["distance"]-buffer > sign*(tp1-entry)+.25*risk]
    tp2 = round_tick(entry+sign*min(later[0]["distance"]-buffer if later else sign*(tp1-entry), 3*risk),
                     cfg.tick, up=sign == -1)
    further = [t for t in later if t["distance"]-buffer > sign*(tp2-entry)+.25*risk]
    tp3 = round_tick(entry+sign*min(further[0]["distance"]-buffer if further else sign*(tp2-entry), 4*risk),
                     cfg.tick, up=sign == -1)
    partials = {"TP1": cfg.partial_tp1, "TP2": cfg.partial_tp2, "TP3": 1-cfg.partial_tp1-cfg.partial_tp2}
    if sign*(tp2-tp1) < cfg.tick:
        partials = {"TP1": 1.0, "TP2": 0.0, "TP3": 0.0}
    elif sign*(tp3-tp2) < cfg.tick:
        partials = {"TP1": cfg.partial_tp1, "TP2": 1-cfg.partial_tp1, "TP3": 0.0}
    if score < cfg.min_score:
        return None, "COMBINED_CONTEXT_SCORE_LOW"
    # A 15m report usually arrives inside a 3m candle. Start PAPER observation
    # on the next whole candle, so its pre-report extremes cannot invent a fill.
    effective = ((s.now+TF["3m"]-1)//TF["3m"])*TF["3m"]
    # Anchor age limits NEW placements; once placed, a fresh full TTL applies.
    # An old 135-minute anchor no longer truncates a 120-minute waiting order.
    if s.now >= a.expires_ts or s.now-a.created_ts > cfg.setup_age_minutes*scale*60_000:
        return None, "ANCHOR_TOO_OLD_TO_PLACE"
    expires = effective+cfg.ttl_minutes*scale*60_000
    if source_tf == '15m':
        expires = min(expires, session_cutoff(s.now,cfg))
    expires = (expires//TF["3m"])*TF["3m"]
    if expires-effective < TF["15m"]:
        return None, "ORDER_LIFETIME_BELOW_REPORT_CADENCE"
    arming = {"distance_atr": distance, "age_minutes": (s.now-a.created_ts)/60_000,
              "runway_r": rr1, "runway": {"targets": targets, "nearest": nearest}, "armable": True}
    candidate = core._arming_candidate(ctx,a,arming,{"setups": {a.setup_type: {"executable": True, "risk_multiplier": 1}}})
    if candidate is None:
        return None, "CANDIDATE_SERIALIZATION_FAILED"
    candidate.final_score = round(score)
    candidate.entry_stage = "CORE"
    candidate.confirmations = [f"Anchor {a.kind} {entry}; {a.reason}",
                               f"HTF 1h={b1}, 4h={b4}; 15m={b15}, 3m={b3}",
                               f"Nearest structural target {nearest['kind']}; net RR {net_rr:.2f}"]
    candidate.score_components["full_v12"] = {"htf": support, "location": location, "regime": regime_score,
                                               "smt": smt_score, "cvd_candle_proxy": cvd_score, "statistic": prior}
    event = anchor_key(a,cfg)
    return {"id": ident(event,s.now), "order_id": ident(event,s.now), "event_key": event,
            "zone_key": event, "instrument": cfg.instrument, "side": sign, "setup": a.setup_type,
            "setup_type": a.setup_type, "setup_family": core.journal_setup_family(a.setup_type),
            "canonical_setup_family": core.canonical_setup_family(a.setup_type), "timeframe": source_tf,
            "setup_timeframe": source_tf, "micro_timeframe": scanner.MICRO[source_tf],
            "entry_policy": "PRETOUCH_LIMIT", "detector_confirmed": True,
            "max_hold_minutes": cfg.max_hold_minutes*scale, "intraday": source_tf == '15m',
            "readiness_pct": 100.0,
            "include_commission":cfg.include_commission,
            "entry_checks":copy.deepcopy(diag['entry_checks']),
            "anchor_id": a.id, "anchor_kind": a.kind, "entry": entry, "limit_price": entry,
            "stop": stop, "initial_stop": stop, "tp": tp1, "tp0": entry+sign*.6*risk,
            "tp1": tp1, "tp2": tp2, "tp3": tp3, "partials": partials,
            "rr1": rr1, "rr2": sign*(tp2-entry)/risk, "rr3": sign*(tp3-entry)/risk,
            "risk": risk, "gross_rr": rr1, "net_rr": net_rr, "costs_r": costs,
            "placement_price": s.price, "target_cancel_armed": sign*(s.price-tp1)<0,
            "score": score, "stop_distance_pct": risk/entry*100,
            "tp1_distance_pct":sign*(tp1-entry)/entry*100,
            "placed_ts": s.now, "effective_ts": effective, "expires_ts": expires, "zone_created_ts": a.created_ts,
            "invalidation": a.invalidation, "target_source": nearest["kind"],
            "evidence": [a.reason]+candidate.confirmations, "candidate": core.candidate_to_dict(candidate),
            "htf_1h": core.side_sign(b1), "htf_4h": core.side_sign(b4),
            "forecast": f"Return to {entry}; defend {a.invalidation}; seek {tp1}, {tp2}, {tp3}",
            "probability": None, "probability_status": "REQUIRES_OUT_OF_SAMPLE_VALIDATION",
            "entry_stage": "CORE", "execution_source": "LIMIT_ARMED_AT_LEVEL",
            "execution": "PAPER_SIGNAL", "status": "PENDING", "last_bar": 0,
            "parent_keys": [], "version": VERSION}, "ACCEPTED"


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
        state["regime_memory"] = copy.deepcopy(ctx.get("regime_memory", {}))
        fresh, detector_audit, matrix = [], [], []
        # The registry order is stable; each original setup runs on native
        # 15m, 1H and 4H bars, not merely on a 15m trend with HTF labels.
        names = [row['setup'] for row in core_setup_registry(core)]
        for setup,detector in zip(names,core.DETECTORS):
            evaluations=[]
            for tf,native in tf_contexts.items():
                try:
                    a = detector(native)
                    row = scanner.conditions(setup,native,core,a)
                    row['setup_conditions']=copy.deepcopy(row.get('conditions',[]))
                    row['setup_readiness_pct']=row.get('readiness_pct',0)
                    scanner.update_readiness(row)
                    if a is not None:
                        a=copy.deepcopy(a)
                        a.evidence['setup_timeframe']=tf
                        a.evidence['micro_timeframe']=scanner.MICRO[tf]
                        a.evidence['setup_conditions']=copy.deepcopy(row['setup_conditions'])
                        # A detector's shorter TTL also scales with its bars.
                        a.expires_ts=a.created_ts+(a.expires_ts-a.created_ts)*scanner.MINUTES[tf]//15
                        fresh.append(a)
                    row.update(timeframe=tf,reason='ACCEPTED' if a else 'SETUP_CONDITIONS_INCOMPLETE',
                               score=a.score if a else 0,score_status='PATTERN' if a else 'UNAVAILABLE',anchor_id=a.id if a else None)
                    row['event_key']=anchor_key(a,cfg) if a else None
                    row.update(scanner.proximity(s.price,row.get('level')))
                    matrix.append(row)
                    evaluations.append({'timeframe':tf,'status':'DETECTED' if a else 'NO_SETUP'})
                except Exception as exc:
                    row={'setup':setup,'timeframe':tf,'status':'ERROR','reason':'DETECTOR_ERROR',
                         'error':type(exc).__name__,'readiness_pct':None,'score':0,
                         'missing_conditions':['Помилка аналізу сетапу'],'level':None,'side':0}
                    matrix.append(row)
                    evaluations.append({'timeframe':tf,'status':'ERROR','error':type(exc).__name__})
            status='ERROR' if any(e['status']=='ERROR' for e in evaluations) else 'DETECTED' if any(e['status']=='DETECTED' for e in evaluations) else 'NO_SETUP'
            detector_audit.append({'detector':detector.__name__,'setup':setup,'status':status,
                                   'timeframes':evaluations})
        anchors = merge_full_anchors(fresh,state,s,cfg)
        stats = setup_statistics(state,cfg)
        plans, watch, refused = [], [], {}
        for a in anchors:
            tf=a.evidence.get('setup_timeframe','15m')
            native=tf_contexts[tf]
            key = anchor_key(a,cfg)
            level_key = ident(a.side,round(a.level/cfg.tick))
            diagnostics={}
            if key in state.get("used_events",{}) or state.get("level_cooldown",{}).get(level_key,0)>s.now:
                p, reason = None, "LEVEL_OR_EVENT_COOLDOWN"
            else:
                p, reason = full_plan(a,native,s,cfg,stats,diagnostics)
                if not p and reason in ('LIMIT_WOULD_CROSS_SPREAD','LIMIT_UNREACHABLE'):
                    diagnostics.update(preview_geometry(a,native,s,cfg))
            row=next((r for r in matrix if r['setup']==a.setup_type and r['timeframe']==tf),None)
            if row is not None and (row['status'] not in ('DETECTED','LIMIT_READY') or row.get('event_key')==key):
                row.update(level=a.level,side=core.side_sign(a.side),anchor_id=a.id,
                           reason=reason,score=p['score'] if p else a.score,
                           status='LIMIT_READY' if p else 'BLOCKED',
                           signal_id=p['id'] if p else None,event_key=key)
                row.update(diagnostics)
                if 'score' in diagnostics:
                    row['score_status']='CONTEXT'
                if a.evidence.get('setup_conditions'):
                    row['setup_conditions']=copy.deepcopy(a.evidence['setup_conditions'])
                elif row.get('setup_readiness_pct',0)<100:
                    row['setup_conditions']=[{'condition':'Патерн підтверджено при формуванні рівня','met':True}]
                scanner.update_readiness(row)
                row.update(scanner.proximity(s.price,a.level))
            if p:
                p["level_key"] = level_key
                plans.append(p)
            else:
                refused[reason] = refused.get(reason,0)+1
                watch.append({"setup": a.setup_type,"timeframe":tf,"readiness_pct":100.,
                              "anchor_id": a.id,"side": core.side_sign(a.side),
                              "level": a.level,"reason": reason,"score": a.score})
    if set(quality.CONTRACTS)!=set(core.DETECTED_SETUP_TYPES):
        raise ValueError('Quality contracts must cover all 24 original setups')
    ledger = quality.ensure(state,cfg)
    # Evaluate every scenario BEFORE combining nearby levels. A high-score
    # unconfirmed continuation must not suppress a valid reversal at that price.
    assessments = {p['id']:quality.assess(p,s,tf_contexts[scanner.timeframe(p)],cfg,
                        sys.modules[__name__],ledger['observations']) for p in plans}
    for p in plans:
        q = assessments[p['id']]
        p['quality'] = q
        checks=p.setdefault('entry_checks',scanner.entry_checks())
        # Accepted planner proposals have already passed the first six gates.
        # This also keeps injected replay planners under the same contract.
        for check in checks:
            if check['key'] not in ('STRUCTURAL_CONFIRMATION','STATISTICAL_FILTER') and check['met'] is None:
                check['met']=True
            if check['key']=='STRUCTURAL_CONFIRMATION':
                check['met']=quality.structural_reason(q['features'])=='CONFIRMED'
            if check['key']=='STATISTICAL_FILTER':
                check['met']=q['reason'] not in ('EMPIRICAL_WIN_RATE_BELOW_TARGET','EMPIRICAL_CONFIDENCE_TOO_LOW','EXPECTANCY_TOO_LOW_AFTER_COSTS')
        p['probability'] = q['statistics']['estimate'] if q['statistics']['observations']>=quality.Settings().minimum_observations else None
        p['probability_status'] = q['status']
        p['forecast'] = 'Confirmed departure; wait for return to limit, preserve structural stop, seek nearest target'
        row=next((r for r in matrix if r.get('signal_id')==p['id']),None)
        if row is not None:
            row.update(reason=q['reason'],status='LIMIT_READY' if q['accepted'] else 'BLOCKED',
                       statistics=q['statistics'])
            row['entry_checks']=copy.deepcopy(checks)
            scanner.update_readiness(row)
        p['readiness_pct']=100. if q['accepted'] else round(100*sum(c['met'] is True for c in checks)/len(checks),1)
        p['distance_pct']=scanner.proximity(s.price,p['entry'])['distance_pct']
    def priority(p):
        q=p['quality'];stat=q['statistics']
        supported=stat['observations']>=quality.Settings().minimum_observations
        structural=quality.structural_reason(q['features'])=='CONFIRMED'
        return (q['accepted'],*scanner.selection_key(p,s.price))
    plans.sort(key=priority,reverse=True)
    clusters = []
    for p in plans:
        cluster = next((q for q in clusters if q["side"] == p["side"]
                        and abs(q["entry"]-p["entry"]) <= max(3*cfg.tick,.15*ctx["atr3"])),None)
        if cluster is None:
            p["supporting_setups"] = [p["setup"]] if p['quality']['accepted'] else []
            p["supporting_families"] = [p["canonical_setup_family"]] if p['quality']['accepted'] else []
            p['alternative_scenarios'] = [{'id':p['id'],'setup':p['setup'],'reason':p['quality']['reason']}]
            p["parent_keys"] = []
            clusters.append(p)
        else:
            cluster['alternative_scenarios'].append({'id':p['id'],'setup':p['setup'],'reason':p['quality']['reason']})
            if p['quality']['accepted'] and p["setup"] not in cluster["supporting_setups"]:
                cluster["supporting_setups"].append(p["setup"])
            cluster["parent_keys"].append(p["event_key"])
            if p['quality']['accepted'] and p["canonical_setup_family"] not in cluster["supporting_families"]:
                cluster["supporting_families"].append(p["canonical_setup_family"])
    # Correlated votes do not manufacture a higher success probability or score.
    clusters.sort(key=priority,reverse=True)
    quality.watch_candidates(state,clusters,assessments,cfg)
    accepted = []
    for p in clusters:
        q = assessments[p['id']]
        if q['accepted']:
            accepted.append(p)
        else:
            refused[q['reason']] = refused.get(q['reason'],0)+1
            watch.append({'setup':p['setup'],'side':p['side'],'level':p['entry'],'reason':q['reason'],'score':p['score']})
    contexts = {tf:{**bias(s.candles[tf]), "side": core.side_sign(core.structure_snapshot(s.candles[tf],60,2)["direction"])}
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
                ('LEVEL_OR_EVENT_COOLDOWN','ANCHOR_INVALIDATED','LEVEL_ALREADY_VISITED_AFTER_OBSERVATION')]
    actionable.sort(key=lambda r:scanner.selection_key(r,s.price),reverse=True)
    watch=actionable[:24]
    return {"health": [],"plans": accepted,"candidate_plans":clusters,"quality_assessments":assessments,
            "watch":watch,"setup_matrix":matrix,"timeframe_contexts":tf_contexts,
            "rejections": refused,"contexts": contexts,"full_context": ctx,
            "detectors": detector_audit,"setup_statistics": stats,
            "raw_plans": len(plans),"registered_setups": len(core.DETECTORS),
            "timeframe_evaluations":len(matrix)}


def core_setup_registry(core):
    # Detector names are not derived from enum names (DIRECTION_FLIP differs).
    from setup_scanner import SETUP_NAMES
    if set(SETUP_NAMES)!=set(core.DETECTED_SETUP_TYPES):
        raise ValueError('Scanner conditions must cover all 24 original setups')
    return [{'setup':name} for name in SETUP_NAMES]






def new_state(cfg: Config) -> dict:
    return {"schema": SCHEMA, "version": VERSION, "instrument": cfg.instrument,
            "config_hash": ident(json.dumps(config_dict(cfg), sort_keys=True)),
            "pending": None, "active": None, "used_events": {}, "trades": [],
            "orders": [], "events": [], "signals": [], "last_run_ts": 0,
            "notification_queue": [], "reconciliation": None,
            "anchor_memory": {}, "level_cooldown": {}, "setup_statistics": {},
            "ledger_revision": 2, "order_sequence": 0,
            "second_slot": {"pending": None, "active": None, "reconciliation": None}}


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


def open_plans(state: dict) -> list[dict]:
    return [view[key] for _,view in slot_views(state) for key in ('pending','active') if view.get(key)]


def occupied_slots(state: dict) -> int:
    return sum(bool(view.get('pending') or view.get('active') or view.get('reconciliation'))
               for _,view in slot_views(state))


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
        for key in ('pending','active'):
            if view.get(key):
                view[key]['slot'] = number


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


def execution_health(s: Snapshot, state: dict) -> list[str]:
    """Lifecycle needs ALL 3m bars since checkpoint, not only a recent window."""
    checkpoint = state.get("last_run_ts", 0)
    rows = s.candles.get("3m", [])
    if not rows:
        return ["NO_3M_FOR_LIFECYCLE"]
    if checkpoint:
        first = (checkpoint//TF["3m"])*TF["3m"]
        needed = [c for c in rows if c.ts >= first]
        if first+TF["3m"] <= s.now and (not needed or needed[0].ts != first):
            return ["LIFECYCLE_HISTORY_GAP"]
        if any(b.ts-a.ts != TF["3m"] for a, b in zip(needed, needed[1:])):
            return ["LIFECYCLE_HISTORY_GAP"]
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
    side, risk = p["side"], p["risk"]
    remaining = p.get("remaining",1.0)
    gross = p.get("realized_gross_r",0)+remaining*side*(price-p["entry"])/risk
    fees = (p["entry"]*p.get("entry_fee_rate",cfg.maker_fee)/risk+p.get("realized_exit_fees_r",0)+remaining*price*cfg.taker_fee/risk) if cfg.include_commission else 0.
    closed = {**copy.deepcopy(p), "status": "CLOSED", "exit": price, "closed_ts": ts,
              "remaining": 0.0, "final_exit_fraction": remaining,
              "closed_at": iso(ts), "close_reason": reason, "gross_r": gross,
              "fees_r": fees, "net_r": gross-fees, "pnl_r": gross-fees,
              "stop_initial": p["initial_stop"], "stop_at_close": p["stop"],
              "bot_version_at_entry": p.get("entry_version", VERSION),
              "architecture_version_at_entry": SCHEMA,
              "result": "WIN" if gross-fees>0 else "LOSS" if gross-fees<0 else "BREAKEVEN",
              "close_action": reason, "ambiguous_ohlc": ambiguous,
              "version": VERSION, "execution": "PAPER_SIGNAL"}
    if not any(t["id"] == closed["id"] for t in state["trades"]):
        state["trades"].append(closed)
    for record in state['orders']:
        if record['id']==p['id']:
            record.update(position_status='CLOSED',closed_ts=ts,trade_net_r=closed['net_r'],
                          trade_result=closed['result'])
    state["active"] = None
    emit(state, "CLOSED", ts, order_id=p["id"], reason=reason, net_r=closed["net_r"],
         ambiguous_ohlc=ambiguous)


def partial_exit(state: dict, p: dict, name: str, level: float, ts: int, cfg: Config) -> None:
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


def manage_bar(state: dict, c: Candle, cfg: Config, fill_bar: bool = False) -> None:
    p = state["active"]
    if not p:
        return
    side, entry, risk = p["side"], p["entry"], p["risk"]
    ts = c.ts+TF["3m"]
    if c.ts <= p.get("last_bar", -1):
        return
    amend = p.get("pending_stop")
    if amend and c.ts >= amend["effective_ts"]:
        p["stop"] = amend["price"]
        p.pop("pending_stop")
    stop_hit = c.low <= p["stop"] if side == 1 else c.high >= p["stop"]
    targets = [(name,p.get(name.lower(),p["tp"])) for name in ("TP1","TP2","TP3")
               if not p.get(name.lower()+"_hit") and p.get("partials",{"TP1":1.0}).get(name,0)>0]
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
    for name,level in targets:
        reached = c.high >= level if side==1 else c.low <= level
        if reached and (not fill_bar or side*(c.close-level)>=0):
            partial_exit(state,p,name,level,ts,cfg)
            if p.get("remaining",1.0) <= 1e-8:
                close_trade(state,p,level*(1-side*cfg.slippage_bps/10_000),ts,"TAKE_PROFIT",cfg)
                return
    if p.get("tp0") and not p.get("tp0_hit"):
        if (c.high>=p["tp0"] if side==1 else c.low<=p["tp0"]) and (not fill_bar or side*(c.close-p["tp0"])>=0):
            p["tp0_hit"] = True
            emit(state,"TP0_MARKER",ts,order_id=p["id"],price=p["tp0"])
    if ts >= p["exit_deadline"]:
        close_trade(state, p, c.close*(1-side*cfg.slippage_bps/10_000), ts, "INTRADAY_TIME_EXIT", cfg)
        return
    if not fill_bar:
        close_r = side*(c.close-entry)/risk
        p["strong_closes"] = p.get("strong_closes", 0)+1 if close_r >= cfg.break_even_trigger_r else 0
    p["last_bar"] = c.ts


def _amend_slot_at_report(state: dict, s: Snapshot, cfg: Config) -> None:
    """Decisions happen at the 15m report, never at historical intra-cycle bars."""
    p = state.get("active")
    if not p or p.get("pending_stop") or (p.get("strong_closes", 0) < 2 and not p.get("tp1_hit")):
        return
    entry, side, risk = p["entry"], p["side"], p["risk"]
    if not p.get("tp1_hit") and side*(s.price-entry)/risk < cfg.break_even_trigger_r:
        return
    slip = cfg.slippage_bps/10_000
    be = entry*(side+fee_rate(cfg,"maker"))/((side-fee_rate(cfg,"taker"))*(1-side*slip))
    be = round_tick(be, cfg.tick, up=side == 1)
    if p.get("tp2_hit"):
        points = pivots(s.candles["3m"][-25:])
        supports = [q for q in points if q["kind"] == ("LOW" if side==1 else "HIGH")]
        if supports:
            trail = supports[-1]["level"]-side*max(2*cfg.tick,.15*atr(s.candles["3m"]))
            if side*(trail-be)>0 and side*(s.price-trail)>.15*risk:
                be = round_tick(trail,cfg.tick,up=side==-1)
    if side*(be-p["stop"]) > 0 and side*(s.price-be) > max(2*cfg.tick, .15*risk):
        effective = ((s.now+TF["3m"]-1)//TF["3m"])*TF["3m"]
        p["pending_stop"] = {"price": be, "effective_ts": effective}
        emit(state, "PAPER_STOP_AMENDED", s.now, order_id=p["id"], stop=be, effective_ts=effective)


def amend_at_report(state: dict, s: Snapshot, cfg: Config) -> None:
    for number,view in slot_views(state):
        _amend_slot_at_report(view,s,cfg)
        store_slot(state,number,view)


def _advance_slot_lifecycle(state: dict, s: Snapshot, cfg: Config) -> None:
    """Chronological replay before applying CURRENT context. No retroactive cancel."""
    for c in s.candles.get("3m", []):
        if state["active"]:
            manage_bar(state, c, cfg)
            continue
        p = state["pending"]
        if not p or c.ts <= p.get("last_bar", -1):
            continue
        end = c.ts+TF["3m"]
        effective = p.get('effective_ts',p['placed_ts'])
        if c.ts < effective:
            if 'effective_ts' not in p and end > p["placed_ts"]:
                possible_fill = c.low < p["entry"]-cfg.tick/2 if p["side"] == 1 else c.high > p["entry"]+cfg.tick/2
                if possible_fill:
                    end_order(state, p, "UNRESOLVED", "PLACEMENT_INSIDE_TOUCHED_BAR", end)
            continue
        cancel_ts = p.get("cancel_effective_ts",p.get("cancel_requested_ts", 0))
        if cancel_ts and c.ts >= cancel_ts:
            end_order(state, p, "CANCELLED", p["cancel_reason"], cancel_ts)
            continue
        if c.ts >= p["expires_ts"]:
            end_order(state, p, "EXPIRED", "LIMIT_TTL", p["expires_ts"])
            continue
        fill = c.low <= p["entry"]-cfg.tick+cfg.tick*1e-8 if p["side"] == 1 else c.high >= p["entry"]+cfg.tick-cfg.tick*1e-8
        if cancel_ts and c.ts < cancel_ts < end:
            end_order(state, p, "UNRESOLVED" if fill else "CANCELLED",
                      "CANCEL_INSIDE_TOUCHED_BAR" if fill else p["cancel_reason"], cancel_ts)
            continue
        # TTL inside a candle: order of fill vs expiry is unknowable from OHLC.
        if end > p["expires_ts"]:
            end_order(state, p, "UNRESOLVED" if fill else "EXPIRED",
                      "EXPIRY_INSIDE_BAR" if fill else "LIMIT_TTL", p["expires_ts"])
            continue
        if fill:
            end_order(state, p, "FILLED", "PAPER_ONE_TICK_PENETRATION", end)
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
            manage_bar(state, c, cfg, fill_bar=True)
        else:
            p["last_bar"] = c.ts
    p = state.get("pending")
    if p and s.now >= p["expires_ts"]:
        # An unfinished bar crossing expiry is not observable yet. Defer until
        # it closes; guessing an expiry could erase a fill before the deadline.
        expiry_bar = (p["expires_ts"]//TF["3m"])*TF["3m"]
        if p["expires_ts"] % TF["3m"] == 0 or s.now >= expiry_bar+TF["3m"]:
            end_order(state, p, "EXPIRED", "LIMIT_TTL", p["expires_ts"])


def advance_lifecycle(state: dict, s: Snapshot, cfg: Config) -> None:
    for number,view in slot_views(state):
        _advance_slot_lifecycle(view,s,cfg)
        store_slot(state,number,view)


def day_stats(state: dict, now: int, cfg: Config) -> dict:
    tz = ZoneInfo(cfg.timezone)
    day = datetime.fromtimestamp(now/1000, tz).date()
    same = lambda ts: datetime.fromtimestamp(ts/1000, tz).date() == day
    trades = [t for t in state["trades"] if same(t["closed_ts"])]
    entries = [o for o in state["orders"] if o["status"] == "FILLED" and same(o["resolved_ts"])]
    # Gross losses cap cannot be replenished by earlier winners.
    losses = sum(min(0, t["net_r"]) for t in trades)
    return {"net_r": sum(t["net_r"] for t in trades), "losses_r": -losses, "entries": len(entries)}


def request_cancel(state: dict, p: dict, reason: str, now: int) -> None:
    if p.get("cancel_requested_ts"):
        return
    if now % TF["3m"] == 0:
        end_order(state, p, "CANCELLED", reason, now)
    else:
        # Current partial candle might contain a fill BEFORE cancellation.
        # Reserve the slot until that candle closes; do not erase that interval.
        p.update(cancel_requested_ts=now, cancel_reason=reason)
        if 'effective_ts' in p:
            p['cancel_effective_ts'] = ((now+TF['3m']-1)//TF['3m'])*TF['3m']
        emit(state, "CANCEL_REQUESTED", now, order_id=p["id"], reason=reason)


def run_cycle(state: dict, s: Snapshot, cfg: Config) -> dict:
    if s.now <= state.get("last_run_ts", 0):
        return {"action": "DUPLICATE_OR_OUT_OF_ORDER", "events": [], "analysis": {}}
    if state["instrument"] != cfg.instrument:
        raise ValueError("Instrument changed: use a separate state path")
    config_hash = ident(json.dumps(config_dict(cfg), sort_keys=True))
    if state.get("config_hash") != config_hash and open_plans(state):
        # One-time release migration: keep EXISTING stop/TP/TTL unchanged.
        # Later operator configuration changes still require closed slots.
        if state.get('version') not in ('full-ict-v12.2.0-confirmed-ict-quality',
                                       'full-ict-v12.3.0-causal-geometry-reports',
                                       'full-ict-v12.4.0-native-mtf-limit-scanner',
                                       'full-ict-v12.4.1-single-scenario-no-commission',
                                       'full-ict-v12.4.2-quote-gates-price-forecast'):
            raise ValueError("Configuration changed with an open plan; finish its lifecycle first")
        state.setdefault('config_migrations',[]).append({'from':state.get('version'),
                 'to':VERSION,'ts':s.now,'existing_plans_preserved':True})
    start = len(state["events"])
    faults = health(s, cfg)
    lifecycle_faults = execution_health(s, state) if open_plans(state) else []
    if lifecycle_faults or any(x in faults for x in ("UNTRUSTED_PRICE", "WRONG_INSTRUMENT", "STALE_TICKER", "STALE_3m")):
        return {"action": "DATA_HOLD", "events": [], "analysis": {"health": faults+lifecycle_faults}}
    release_unconfirmed_paper(state,s.now,cfg)
    bind_order_numbers(state)
    advance_lifecycle(state, s, cfg)
    amend_at_report(state, s, cfg)
    quality.advance(state,s,cfg,sys.modules[__name__])
    # A non-PAPER reconciliation still needs a human check. Existing positions
    # continue to be managed; one uncertain interval never erases another plan.
    unresolved = [view['reconciliation'] for _,view in slot_views(state) if view.get('reconciliation')]
    def needs_manual_check(r):
        record = next((o for o in reversed(state['orders']) if o['id']==r['order_id']),{})
        return r.get('reason') not in PAPER_UNCERTAINTY or record.get('execution')!='PAPER_SIGNAL'
    manual_block = any(needs_manual_check(r) for r in unresolved)
    analysis = analyze(s, cfg, state) if not manual_block else {"health":faults,"plans":[],"watch":[],"rejections":{},"contexts":{}}
    state["setup_statistics"] = analysis.get("setup_statistics",state.get("setup_statistics",{}))
    if analysis.get('full_context'):
        quality.refresh_confirmations(state,s,analysis['full_context'],cfg,sys.modules[__name__],
                                      analysis.get('timeframe_contexts'))
    ctx = analysis.get("contexts", {})
    for number,view in slot_views(state):
        pending = view.get("pending")
        if pending and ctx:
            if pending["side"]*(s.price-pending["tp"]) < 0:
                pending["target_cancel_armed"] = True
            opposite = -pending["side"]
            if (cfg.cancel_on_bias_flip and ctx["1H"]["side"] == opposite and ctx["4H"]["side"] == opposite
                    and not (pending.get("htf_1h")==opposite and pending.get("htf_4h")==opposite)):
                request_cancel(view, pending, "CONFIRMED_HTF_FLIP", s.now)
            elif pending["side"]*(s.price-pending["invalidation"]) <= 0:
                request_cancel(view, pending, "STRUCTURE_INVALIDATED_NOW", s.now)
            elif pending.get("target_cancel_armed",True) and pending["side"]*(s.price-pending["tp"]) >= 0:
                request_cancel(view, pending, "TARGET_REACHED_WITHOUT_FILL", s.now)
        store_slot(state,number,view)
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
    reserved = sum(bool(view.get('pending')) for _,view in slot_views(state))
    can_place = occupied_slots(state)<2 and not manual_block
    if can_place and analysis['health']:
        if not busy:
            action = 'DATA_HOLD'
        can_place = False
    elif can_place and s.now >= session_cutoff(s.now,cfg)-TF['15m'] and not any(
            scanner.timeframe(p) != '15m' for p in analysis.get('plans',[])):
        if not busy:
            action = 'SESSION_CLOSED'
        can_place = False
    elif can_place and (daily['losses_r']>=cfg.day_loss_cap_r or daily['entries']+reserved>=cfg.max_daily_trades):
        if not busy:
            action = 'DAILY_PAUSE'
        can_place = False
    if can_place:
        plans = sorted(analysis.get('plans',[]),key=lambda p:scanner.selection_key(p,s.price),reverse=True)
        sides = {p['side'] for p in busy}
        if not sides and plans:
            top = plans[0]
            sides = {top['side']}
        # Opposing proposals never hedge or replace an existing thesis.
        for top in plans if can_place and len(sides)==1 else []:
            busy = open_plans(state)
            if occupied_slots(state)>=2 or daily['entries']+reserved>=cfg.max_daily_trades:
                break
            keys = {top['event_key'],*top.get('parent_keys',[])}
            live_keys = {key for p in busy for key in (p['event_key'],*p.get('parent_keys',[]))}
            tolerance = max(3*cfg.tick,.15*atr(s.candles.get('3m',[])))
            if top['side'] not in sides or keys.intersection(state['used_events']) or keys.intersection(live_keys):
                continue
            if any(p['side']==top['side'] and abs(p['entry']-top['entry'])<=tolerance for p in busy):
                continue
            for number,view in slot_views(state):
                if view.get('pending') or view.get('active') or view.get('reconciliation'):
                    continue
                plan = copy.deepcopy(top)
                # Also enforce timing for injected/replay planner proposals.
                plan['placed_ts'] = s.now
                plan.setdefault('effective_ts',((s.now+TF['3m']-1)//TF['3m'])*TF['3m'])
                plan.setdefault('target_cancel_armed',plan['side']*(s.price-plan['tp'])<0)
                plan['slot'] = number
                view['pending'] = plan
                store_slot(state,number,view)
                bind_order_numbers(state)
                selected.append(plan)
                reserved += 1
                emit(state,'PLACED',s.now,order_id=plan['id'],side=plan['side'],entry=plan['entry'],
                     stop=plan['stop'],tp=plan['tp'],effective_ts=plan['effective_ts'])
                action = 'PLACE_LIMIT'
                break
            if selected:
                break  # One best new scenario per cycle, at most two live slots.
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
        elif live and row.get('side') not in {p['side'] for p in live}:
            row.update(status='BLOCKED',reason='EXISTING_OPPOSITE_PLAN')
        elif action in ('DIRECTION_CONFLICT','DAILY_PAUSE','SESSION_CLOSED','DATA_HOLD'):
            row.update(status='BLOCKED',reason=action)
    if analysis.get('setup_matrix'):
        analysis['watch']=[w for w in analysis.get('watch',[]) if w.get('status')!='FOLLOWED']
    sides={p['side'] for p in live} or None
    selected_row=next((row for row in analysis.get('setup_matrix',[]) if selected
                       and row.get('signal_id')==selected[0]['id']),None)
    analysis['best_scenario']=selected_row or scanner.best_scenario(analysis.get('watch',[]),s.price,sides)
    if analysis['best_scenario']:
        best=analysis['best_scenario']
        best['price_forecast']=price_forecast.forecast(best,s,cfg)
    for number,view in slot_views(state):
        if view.get('pending'):
            plan=view['pending']
            plan['price_forecast']=price_forecast.forecast(plan,s,cfg)
            store_slot(state,number,view)
    state['best_scenario']=copy.deepcopy(analysis['best_scenario'])
    state['last_run_ts'] = s.now
    state['config_hash'] = config_hash
    state['include_commission']=cfg.include_commission
    state['price_tick']=cfg.tick
    state['version'] = VERSION
    state['ledger_revision'] = 2
    cutoff = s.now-2*86_400_000
    state['used_events'] = {k:v for k,v in state['used_events'].items() if v>=cutoff}
    state['level_cooldown'] = {k:v for k,v in state.get('level_cooldown',{}).items() if v>s.now}
    # Keep IDs for every followed plan, not only the newly selected candidate.
    summary = {'ts':s.now,'time':iso(s.now),'action':action,'price':s.price,
               'plan_id':selected[0]['id'] if selected else None,
               'placed_ids':[p['id'] for p in selected],
               'followed_ids':[p['id'] for p in open_plans(state)],
               'occupied_slots':occupied_slots(state),'max_slots':2,
               'rejections':analysis.get('rejections',{}),'health':analysis['health'],'daily':daily,
               'detectors':analysis.get('detectors',[]),'raw_plans':analysis.get('raw_plans',0),
               'registered_setups':analysis.get('registered_setups',24),
               'timeframe_evaluations':analysis.get('timeframe_evaluations',0)}
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
    names = [name for name in ('TP1','TP2','TP3') if p.get('partials',{'TP1':1}).get(name,0)>0]
    return ' • '.join(f"{name} {p.get(name.lower(),p['tp']):.6g}"
                    + (' (досягнуто)' if p.get(name.lower()+'_hit') else '') for name in names)


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
                'TAKE_PROFIT':'усі тейки','INTRADAY_TIME_EXIT':'кінець часу угоди'}.get(reason,'завершення')
        value = event['net_r']
        outcome = 'прибуток' if value>0 else 'збиток' if value<0 else 'беззбитково'
        achieved = [name for name in ('TP1','TP2','TP3') if trade.get(name.lower()+'_hit')]
        details = ', '.join(achieved)+' досягнуто; ' if achieved else 'тейків не досягнуто; '
        uncertainty = ' • порядок SL/TP у свічці невідомий, узято стоп' if event.get('ambiguous_ohlc') else ''
        emoji='✅' if value>0 else '🔴' if value<0 else '⚪'
        return f"{emoji} {at} {label} ЗАКРИТО: {value:+.2f}R — {outcome}\n   {details}{text}{uncertainty}"
    if kind=='FILLED':
        return f"🟢 {at} {label} ЦІНА ПРОЙШЛА ЛІМІТ — супровід сигналу розпочато"
    if kind in ('TP1','TP2','TP3'):
        return f"🎯 {at} {label} {kind} досягнуто — зафіксовано {event['fraction']*100:.0f}% позиції"
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
    'SLOTS_FULL':'обидва місця зайняті; потрібне закриття одного ордера',
    'EXISTING_OPPOSITE_PLAN':'вже супроводжуємо протилежний напрямок',
    'DIRECTION_CONFLICT':'протилежні сценарії мають близьку оцінку',
    'DAILY_PAUSE':'денний ліміт ризику або кількості входів',
    'SESSION_CLOSED':'наступна торгова сесія для 15m входу',
    'DATA_HOLD':'відновлення повних актуальних даних',
    'LIMIT_WOULD_CROSS_SPREAD':'поточні ASK/BID не дозволяють пасивний ліміт',
    'LIMIT_UNREACHABLE':'рівень далі допустимої відстані у ATR',
    'INVALID_STRUCTURAL_FALSIFIER':'немає коректного рівня скасування сетапу',
    'ANCHOR_INVALIDATED':'структура сетапу порушена',
    'LEVEL_ALREADY_VISITED_AFTER_OBSERVATION':'рівень уже відвідано після виявлення',
    'STRUCTURAL_STOP_TOO_WIDE':'структурний стоп перевищує допустимий ризик',
    'NEAREST_TARGET_TOO_CLOSE_AFTER_COSTS':'потенціал TP відносно SL нижчий за мінімум',
    'TARGET_RR_TOO_LOW':'потенціал TP відносно SL нижчий за мінімум',
    'TARGET_DISTANCE_TOO_SMALL':'найближча структурна ціль ближча за мінімальний TP1 1.5%',
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
    'EMPIRICAL_WIN_RATE_BELOW_TARGET':'історична оцінка win rate нижча порогу',
    'EMPIRICAL_CONFIDENCE_TOO_LOW':'історична вибірка недостатньо переконлива',
    'EXPECTANCY_TOO_LOW_AFTER_COSTS':'історичний результат після витрат надто низький',
    'ANCHOR_TOO_OLD_TO_PLACE':'сетап утратив актуальність',
    'ORDER_LIFETIME_BELOW_REPORT_CADENCE':'до завершення сесії замало часу',
    'LEVEL_OR_EVENT_COOLDOWN':'цей рівень уже використаний; чекаємо новий сетап',
    'ANCHOR_COOLDOWN':'рівень ще на паузі',
    'STOP_INSIDE_NOISE':'стоп усередині цінового шуму',
    'DETECTOR_ERROR':'помилка аналізу цього сетапу',
}


def expected_line(stat: dict, forecast=None) -> str:
    n=stat.get('observations',0)
    if n>=quality.Settings().minimum_observations and stat.get('estimate') is not None:
        scope='сетап' if stat.get('scope')=='setup' else 'сім’я сетапів'
        return (f"📈 Оцінка win rate: {stat['estimate']*100:.1f}% • {scope}, n={n}"
                +f" • нижня межа 95%: {stat['lower_95']*100:.1f}%")
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
    local = datetime.fromtimestamp(s.now/1000,ZoneInfo(cfg.timezone))
    symbol=cfg.instrument.removesuffix('-SWAP')
    price_ok=s.trusted and s.price>0 and 0<=s.now-s.ticker_ts<=120_000
    current_price=f'{s.price:.6g}' if price_ok else 'недоступна'
    lines = [f"📊 {symbol} • {local:%d.%m %H:%M} (Київ) • {REPORT_RELEASE}",
             f"Ціна зараз: {current_price} • Зайнято місць: {occupied_slots(state)}/2"]
    new_ids = {p['id'] for p in result.get('selected_plans',[])}
    if result.get('selected'):
        new_ids.add(result['selected']['id'])
    live_keys=set()
    for number,view in slot_views(state):
        p = view.get('active') or view.get('pending')
        if p:
            live_keys.add((p.get('setup'),scanner.timeframe(p)))
            label = display_order(state,p['id'])
            side = 'LONG' if p['side']==1 else 'SHORT'
            # A price-signal book cannot certify exchange fills. Avoid the old
            # banners and label the concrete lifecycle as a SIGNAL once.
            status = 'СУПРОВІД СИГНАЛУ' if view.get('active') else 'НОВИЙ ЛІМІТНИЙ СИГНАЛ' if p['id'] in new_ids else 'ЛІМІТНИЙ СИГНАЛ — ЧЕКАЄМО'
            lines += ['',f"{label} {side} • {status}",
                      f"{p.get('setup','Сетап')} • {scanner.timeframe(p).lower()}",
                      f"Вхід: {p['entry']:.6g} • SL: {p['stop']:.6g} ({p.get('stop_distance_pct',p['risk']/p['entry']*100):.2f}%)",
                      target_line(p)]
            if view.get('pending'):
                distance=scanner.proximity(s.price if price_ok else 0,p['entry'],p.get('placement_price'))
                detail=scanner.distance_text(s.price,p['entry'],cfg.tick) if price_ok else '📏 До входу: немає свіжої ціни'
                if distance['approach_pct'] is not None and s.price!=p['entry']:
                    detail+=f" • наближення {distance['approach_pct']:.0f}%"
                lines.append(detail)
                if price_ok and s.price==p['entry']:
                    lines.append('⏳ Ліміт активний; підтвердження проходу ціни перевіряється на наступних свічках')
                elif price_ok:
                    move='зниження' if s.price>p['entry'] else 'зростання'
                    lines.append(f"🚦 Чекаємо {move} до ціни входу {p['entry']:.8g}")
                lines += ["Готовність до входу на момент сигналу: 100%",
                          f"⏱ Ліміт діє до {report_deadline(p['expires_ts'],s.now,cfg)}"]
                if p.get('cancel_requested_ts'):
                    lines.append('Скасування: '+REASON_UA.get(p.get('cancel_reason'),'сетап утратив чинність'))
            else:
                remaining = p.get('remaining',1)
                gross = p.get('realized_gross_r',0)+remaining*p['side']*(s.price-p['entry'])/p['risk']
                fees = (p['entry']*p.get('entry_fee_rate',cfg.maker_fee)/p['risk']+p.get('realized_exit_fees_r',0)+remaining*s.price*cfg.taker_fee/p['risk']) if cfg.include_commission else 0.
                slip = remaining*s.price*cfg.slippage_bps/10_000/p['risk']
                estimate=f'{gross-fees-slip:+.2f}R' if price_ok else 'н/д — немає свіжої ціни'
                lines.append(f"Залишилось {remaining*100:.0f}% • оцінний результат: {estimate}")
                if p.get('pending_stop'):
                    amend = p['pending_stop']
                    lines.append(f"Новий SL {amend['price']:.6g} з {local_time(amend['effective_ts'],cfg)}")
                lines.append(f"Завершити до {report_deadline(p['exit_deadline'],s.now,cfg)}")
            lines.append(expected_line(p.get('quality',{}).get('statistics',{}),p.get('price_forecast') if view.get('pending') and price_ok and not result.get('analysis',{}).get('health') else None))
        if view.get('reconciliation'):
            r = view['reconciliation']
            lines += ['',f"{display_order(state,r['order_id'])} — РЕЗУЛЬТАТ НЕВИЗНАЧЕНИЙ",
                      'За свічками не можна встановити порядок виконання.']
            if r.get('release_ts'):
                lines.append(f"Місце зарезервовано до {report_deadline(r['release_ts'],s.now,cfg)}")
    events = [message_event(state,e,cfg) for e in result.get('events',[])]
    events = [e for e in events if e]
    if events:
        lines += ['','Зміни:']+events[-8:]
    analysis=result.get('analysis',{})
    watches=analysis.get('watch',state.get('watch',[])) if price_ok and not analysis.get('health') else []
    live_sides={p['side'] for p in open_plans(state)} or None
    best=analysis.get('best_scenario') or scanner.best_scenario(watches,s.price,live_sides)
    if best and ((best.get('setup'),best.get('timeframe','15m')) in live_keys or new_ids):
        best=None  # The chosen live signal has already been shown above.
    if best is None and not open_plans(state) and price_ok and not analysis.get('health'):
        best=scanner.best_scenario(analysis.get('setup_matrix',[]),s.price)
    if best is not None:
        w=best
        side='LONG' if w.get('side')==1 else 'SHORT'
        ready=w.get('readiness_pct',0) or 0.
        lines += ['','Найкращий сценарій:',f"{w['setup']} • {w.get('timeframe','15m').lower()} • {side}"]
        count=f" ({w['met_conditions']}/{w['total_conditions']})" if 'met_conditions' in w else ''
        lines.append(f"Готовність до входу: {ready:.1f}%{count}")
        if w.get('score'):
            label='Якість контексту' if w.get('score_status')=='CONTEXT' else 'Оцінка патерну' if w.get('score_status')=='PATTERN' else 'Оцінка'
            lines.append(f"{label}: {w['score']:.1f}/100")
        if w.get('level'):
            lines.append(f"🎯 Вхід: {w['level']:.8g}")
            lines.append(scanner.distance_text(s.price,w['level'],cfg.tick))
        if w.get('stop'):
            distance=abs(w['stop']-w.get('entry',w.get('level',0)))/w.get('entry',w['level'])*100
            if w.get('tp1'):
                lines.append(f"SL: {w['stop']:.6g} ({distance:.2f}%) • TP1: {w['tp1']:.6g}")
            elif w.get('required_tp'):
                lines.append(f"SL: {w['stop']:.6g} ({distance:.2f}%) • TP1 потрібен {'≥' if w['side']==1 else '≤'} {w['required_tp']:.8g}")
        reason=w.get('reason')
        if reason=='LIMIT_WOULD_CROSS_SPREAD' and w.get('level'):
            lines.append(scanner.wait_text(w['level'],w['side'],s.bid,s.ask,cfg.tick))
        elif reason=='TARGET_DISTANCE_TOO_SMALL':
            lines.append(f"Вхід заблоковано: до найближчої цілі {w['available_tp1']:.8g} лише {w['tp1_distance_pct']:.2f}%; мінімум {w.get('min_target_pct',cfg.min_target_pct)*100:.1f}%")
        elif reason in ('TARGET_RR_TOO_LOW','NEAREST_TARGET_TOO_CLOSE_AFTER_COSTS'):
            if w.get('gross_rr') is not None:
                lines.append(f"Вхід заблоковано: TP дає {w['gross_rr']:.2f}R; мінімум {w.get('required_rr',cfg.min_net_rr):.2f}R")
                if w.get('required_tp') and w['required_tp']>0:
                    lines.append(f"🎯 При цьому SL потрібен структурний TP {'≥' if w['side']==1 else '≤'} {w['required_tp']:.8g}")
            else:
                lines.append('Вхід заблоковано: потенціал TP відносно SL нижчий за мінімум')
        elif reason in ('CONFIRMED','ACCEPTED'):
            lines.append('Умови входу виконані; очікуємо активацію ліміту')
        elif reason=='SETUP_CONDITIONS_INCOMPLETE':
            pattern=[c['condition'] for c in w.get('setup_conditions',w.get('conditions',[])) if c.get('met') is not True]
            lines.append('Чекаємо: '+('; '.join(pattern[:2]) or '; '.join(w.get('missing_conditions',[])[:2]) or 'підтвердження патерну'))
        else:
            lines.append('Чекаємо: '+REASON_UA.get(reason,'підтвердження допустимості входу'))
        lines.append(expected_line(w.get('statistics',{}),w.get('price_forecast')))
    reasons = {'DIRECTION_CONFLICT':'Вхід відкладено: однаково сильні протилежні сценарії.',
               'DAILY_PAUSE':'Нові входи на паузі: денний ліміт ризику або угод.',
               'SESSION_CLOSED':'Нові 15m входи — з наступної сесії.',
               'DATA_HOLD':'Аналіз призупинено: ринкові дані неповні або застарілі.',
               'DUPLICATE_OR_OUT_OF_ORDER':'Цей звіт уже оброблено.'}
    if result['action'] in reasons:
        lines += ['',reasons[result['action']]]
    if result['action']=='DATA_HOLD':
        lines.append('Причина: '+', '.join(analysis.get('health',[])))
    if occupied_slots(state)==2:
        lines.append('Новий ордер — після звільнення одного місця.')
    # A fixed Telegram limit. Drop whole optional scenario cards first; all
    # lifecycle events remain durably available in the journal/dashboard.
    message=decorate_message('\n'.join(lines))
    if len(message)>4000:
        message=message[:3950].rsplit('\n',1)[0]+'\nПовні деталі — у журналі.'
    return message


def decorate_message(message):
    # Also normalizes inline TP marks and queued messages from older releases.
    # Keep price/math symbols (•, %, ≥, ≤, ≈, +/-) and event text intact.
    icons=[('Ціна зараз:','💰'),('Найкращий сценарій:','🏆'),('Готовність до входу','🧩')]
    def is_icon(char):
        point=ord(char)
        return (0x1F000<=point<=0x1FAFF or 0x2300<=point<=0x23FF
                or 0x2600<=point<=0x27BF or point in (0xFE0F,0x200D,0x20E3))
    output=[]
    for line in message.split('\n'):
        stripped=''.join(char for char in line if not is_icon(char)).strip()
        icon=next((mark for start,mark in icons if stripped.startswith(start)),None)
        output.append(icon+' '+stripped if icon else stripped)
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
            json.dump(data, handle, ensure_ascii=False, indent=2, allow_nan=False)
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
                      "second_slot": state.get('second_slot'), "open_plans": open_plans(state),
                      "occupied_slots": occupied_slots(state), "max_slots": 2,
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
                      "statistics": statistics(state["trades"]),"setup_statistics": state.get("setup_statistics",{})}
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






def notify_queue(state: dict, token: str, chat_id: str, path: Path) -> bool:
    if not token or not chat_id:
        raise ValueError("Telegram token/chat_id required for --notify")
    while state["notification_queue"]:
        item = state["notification_queue"][0]
        body=item['text']
        if not item.get('version') and ('Найближчі сетапи:' in body or 'після комісій' in body):
            body='🕓 Збережений звіт старого формату з черги\n'+body
        body=decorate_message(body)
        try:
            data = http_json("https://api.telegram.org/bot"+token+"/sendMessage",
                             {"chat_id": chat_id, "text": body})
            if not data.get("ok"):
                print("Telegram rejected the message; queued for retry", file=sys.stderr)
                return False
        except Exception:
            # Never print URLs / request exceptions containing the bot token.
            print("Telegram unavailable; message retained for retry", file=sys.stderr)
            return False
        state["notification_queue"].pop(0)
        atomic_write(path, state)
    return True


def replay(data: dict, cfg: Config) -> dict:
    """15m causal replay. Warmup precedes optional start_ts; no future HTF bars."""
    state = new_state(cfg)
    raw = {tf: sorted([Candle.parse(r) for r in data["candles"].get(tf, [])], key=lambda c:c.ts)
           for tf in TF}
    raw = {tf: sorted({c.ts: c for c in rows if c.confirmed}.values(), key=lambda c:c.ts)
           for tf, rows in raw.items()}
    smt_rows = [Candle.parse(r) for r in data.get("smt_candles_15m",[])]
    smt_rows = sorted({c.ts:c for c in smt_rows if c.confirmed}.values(),key=lambda c:c.ts)
    smt_available = [c.ts+TF["15m"] for c in smt_rows]
    available = {tf: [c.ts+TF[tf] for c in rows] for tf, rows in raw.items()}
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
    raw = data.get('candles',{}).get('3m',[])
    if checkpoint and raw:
        for _ in range(24):
            earliest = min(c.ts for c in raw)
            if earliest <= checkpoint:
                break
            url = f"{core.OKX_BASE_URL}/history-candles?"+urllib.parse.urlencode(
                {'instId':cfg.instrument,'bar':'3m','after':earliest,'limit':300})
            resp = core.http_get(url)
            body = resp.json() if resp else {}
            older = [Candle.parse(row) for row in body.get('data',[])] if body.get('code')=='0' else []
            if not older or min(c.ts for c in older)>=earliest:
                break
            raw.extend(older)
    data['candles']['3m'] = raw
    data['now'] = int(time.time()*1000)
    data['trusted'] = data.get('execution_price_trusted',False)
    data['candles'] = {tf:[asdict(c) for c in rows] for tf,rows in data.get('candles',{}).items()}
    data['smt_candles_15m'] = [asdict(c) for c in data.get('smt_candles_15m',[])]
    return Snapshot.parse(data)


def run_live(core, snapshot_path: Optional[Path] = None, notify: bool = False, ack: bool = False) -> int:
    if core.BOT_VERSION != VERSION:
        raise ValueError('Неповне оновлення: оновіть bot_oneshot.py і full_limit_engine.py з одного архіву')
    expected=os.environ.get('EXPECTED_BOT_VERSION')
    if expected and expected != VERSION:
        raise ValueError('Workflow очікує іншу версію коду: оновіть весь ZIP')
    cfg = replace(Config.from_env(),core=core)
    state_path,journal_path = Path(core.STATE_FILE),Path(core.JOURNAL_FILE)
    if state_path.resolve()==journal_path.resolve():
        raise ValueError('State and journal paths must differ')
    if notify and (not core.TELEGRAM_TOKEN or not core.TELEGRAM_CHAT_ID):
        raise ValueError('Telegram credentials required before starting --notify')
    with locked(Path(str(state_path)+'.lock')):
        if snapshot_path is None:
            cfg = resolve_live_config(cfg)
        state = load_state(state_path,cfg)
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
        snap = Snapshot.parse(read_json(snapshot_path)) if snapshot_path else collect_snapshot(cfg,state['last_run_ts'])
        old_events = len(state.get('events',[]))
        # A separate scan runs every five minutes; deliver all its lifecycle
        # changes in the NEXT 15-minute report, never send a Telegram every 5m.
        event_watermark = min(state.get('last_notified_event_count',old_events),old_events)
        result = run_cycle(state,snap,cfg)
        if notify:
            result['events'] = sorted(state['events'][event_watermark:],key=lambda e:e['ts'])
        message = build_message(state,snap,result,cfg) if notify else ''
        state.setdefault('last_notified_event_count',old_events)
        if notify and result['action']!='DUPLICATE_OR_OUT_OF_ORDER':
            state['last_notified_event_count']=len(state['events'])
        if message:
            print(message)
        if result['action']=='DUPLICATE_OR_OUT_OF_ORDER':
            export_journal(journal_path,state,core)
            return 0 if not notify or notify_queue(state,core.TELEGRAM_TOKEN,core.TELEGRAM_CHAT_ID,state_path) else 2
        if notify:
            state['notification_queue'].append({'id':ident(snap.now,message),'text':message,'version':VERSION,'ts':snap.now})
        atomic_write(state_path,state)
        export_journal(journal_path,state,core)
        if notify and not notify_queue(state,core.TELEGRAM_TOKEN,core.TELEGRAM_CHAT_ID,state_path):
            return 2
        if result['action']=='DATA_HOLD':
            print('DATA_HOLD: no new orders; Telegram summary/state were processed',file=sys.stderr)
        return 0  # preserve queued notifications/ledger even when data are on hold


def main(core) -> int:
    parser = argparse.ArgumentParser(description='Full 24-setup ICT LIMIT bot v12')
    parser.add_argument('--self-test',action='store_true')
    parser.add_argument('--legacy-self-test',action='store_true',help='Original compatibility checks; no production execution')
    parser.add_argument('--audit-journal',type=Path)
    parser.add_argument('--replay',type=Path)
    parser.add_argument('--snapshot',type=Path)
    parser.add_argument('--output',type=Path)
    parser.add_argument('--notify',action='store_true')
    parser.add_argument('--ack-unresolved',action='store_true')
    args = parser.parse_args()
    cfg = replace(Config.from_env(),core=core)
    if args.legacy_self_test:
        return 0 if core._run_self_test() else 1
    if args.self_test:
        import unittest
        suite = unittest.defaultTestLoader.discover(str(ROOT),pattern='test_*.py')
        if not suite.countTestCases():
            raise ValueError('test_full_limit_engine.py required')
        return 0 if unittest.TextTestRunner(verbosity=2).run(suite).wasSuccessful() else 1
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
                            and core.TELEGRAM_NOTIFY_EVERY_RUN and not args.snapshot)
    return run_live(core,args.snapshot,notify,args.ack_unresolved)
