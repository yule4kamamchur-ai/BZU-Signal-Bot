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

VERSION = "full-ict-v12.0.0-all-24-limit-optimizer"
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
    slippage_bps: float = 2.0
    min_net_rr: float = 1.20
    max_target_r: float = 2.0
    max_stop_atr: float = 2.0
    min_stop_atr3: float = 0.65
    max_entry_distance_atr: float = 3.0
    min_score: float = 60.0
    conflict_margin: float = 8.0
    ttl_minutes: int = 90
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
        if self.min_stop_atr3 <= 0 or self.max_stop_atr <= 0:
            raise ValueError("Invalid stop bounds")
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
    return {f.name: getattr(cfg, f.name) for f in fields(cfg) if f.name != "core"}


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
    return (entry*cfg.maker_fee + exit_price*cfg.taker_fee)/risk


REVERSALS = frozenset({"SWEEP_RECLAIM", "CAPITULATION_RECOVERY", "RANGE_EDGE_REVERSAL",
                      "FAILED_AUCTION_REJECTION", "LIQUIDITY_SWEEP_REVERSAL_SHORT",
                      "BUYER_EXHAUSTION_SHORT", "FAILED_BREAKOUT_SHORT", "MSS_REVERSAL_SHORT",
                      "FAILED_OPENING_RANGE_BREAKOUT", "OR_FAILURE_2_SHORT", "DIRECTION_FLIP_15M"})


def setup_statistics(state: dict, cfg: Config) -> dict:
    """Only this execution architecture can calibrate its own setup ranking."""
    report = {}
    for setup in cfg.core.DETECTED_SETUP_TYPES:
        rows = [t for t in state.get("trades", []) if t.get("setup_type") == setup
                and t.get("bot_version_at_entry",t.get("entry_version")) == VERSION
                and not t.get("ambiguous_ohlc")]
        stat = statistics(rows)
        n = stat["trades"]
        status = "INSUFFICIENT_SAMPLE" if n < cfg.setup_min_sample else (
            "NEGATIVE_EXPECTANCY" if stat["expectancy_r"] <= 0 else "POSITIVE_EXPECTANCY")
        report[setup] = {**stat, "status": status, "required_sample": cfg.setup_min_sample,
                         "version": VERSION, "probability_validated": False}
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
    return ident(a.setup_type, a.side, round(a.level/cfg.tick), round(a.invalidation/cfg.tick), a.kind)


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
        a.expires_ts = min(a.expires_ts, a.created_ts+cfg.setup_age_minutes*60_000)
        memory[key] = core.anchor_to_dict(a)
    # Retain tombstones beyond setup lifetime to prevent a detector refreshing age.
    memory = {k: v for k, v in memory.items() if s.now-v["created_ts"] <= 2*86_400_000}
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


def full_plan(a, ctx: dict, s: Snapshot, cfg: Config, stats: dict) -> tuple[Optional[dict], str]:
    core = cfg.core
    side = str(a.side)
    sign = core.side_sign(side)
    a15, a3 = ctx["atr15"], ctx["atr3"]
    if not sign or min(a15, a3) <= 0:
        return None, "INVALID_DIRECTION_OR_VOLATILITY"
    if a.cooldown_until_ts > s.now:
        return None, "ANCHOR_COOLDOWN"
    entry = round_tick(a.level, cfg.tick, up=sign == -1)
    if (sign == 1 and entry >= s.bid) or (sign == -1 and entry <= s.ask):
        return None, "LIMIT_WOULD_CROSS_SPREAD"
    distance = sign*(s.price-entry)/a15
    if not 0 < distance <= cfg.max_entry_distance_atr:
        return None, "LIMIT_UNREACHABLE"
    if sign*(entry-a.invalidation) <= 0:
        return None, "INVALID_STRUCTURAL_FALSIFIER"
    observed = [c for c in s.candles["3m"] if c.ts >= a.created_ts]
    if any(sign*(c.close-a.invalidation) <= 0 for c in observed) or sign*(s.price-a.invalidation) <= 0:
        return None, "ANCHOR_INVALIDATED"
    if any(c.low <= entry if sign == 1 else c.high >= entry for c in observed):
        return None, "LEVEL_ALREADY_VISITED_AFTER_OBSERVATION"
    buffer = max(.15*a3, 2*cfg.tick)
    stop = round_tick(a.invalidation-sign*buffer, cfg.tick, up=sign == -1)
    risk = sign*(entry-stop)
    if risk < max(cfg.min_stop_atr3*a3, 4*cfg.tick):
        return None, "STOP_INSIDE_NOISE"
    if risk > cfg.max_stop_atr*a15:
        return None, "STRUCTURAL_STOP_TOO_WIDE"
    b1 = core.structure_snapshot(s.candles["1H"], 60, 2)["direction"]
    b4 = core.structure_snapshot(s.candles["4H"], 60, 2)["direction"]
    b15, b3 = ctx["structure15"]["direction"], ctx["structure3m"]["direction"]
    opposite = "SHORT" if sign == 1 else "LONG"
    both_against = b1 == opposite and b4 == opposite
    reversal_confirmed = a.setup_type in REVERSALS and b15 == side and b3 == side
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
    if not targets:
        return None, "NO_STRUCTURAL_TARGET"
    nearest = targets[0]
    tp1 = entry+sign*min(nearest["distance"]-buffer, cfg.max_target_r*risk)
    tp1 = round_tick(tp1, cfg.tick, up=sign == -1)
    rr1 = sign*(tp1-entry)/risk
    costs = fees_r(entry, tp1, risk, cfg)+tp1*cfg.slippage_bps/10_000/risk
    net_rr = rr1-costs
    if net_rr < cfg.min_net_rr:
        return None, "NEAREST_TARGET_TOO_CLOSE_AFTER_COSTS"
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
    location = max(0, 100-22*distance)+min(15, 5*net_rr)
    score = .35*a.score+.25*min(100,max(0,support))+.20*min(100,location)+.10*regime_score+.05*smt_score+.05*cvd_score
    prior = stats.get(a.setup_type, {})
    if prior.get("status") == "NEGATIVE_EXPECTANCY":
        score -= min(12, abs(prior["expectancy_r"])*10)
    elif prior.get("status") == "POSITIVE_EXPECTANCY":
        score += min(5, prior["expectancy_r"]*5)
    score = round(min(100,max(0,score)), 2)
    if score < cfg.min_score:
        return None, "COMBINED_CONTEXT_SCORE_LOW"
    expires = min(a.expires_ts, s.now+cfg.ttl_minutes*60_000, session_cutoff(s.now,cfg))
    if expires-s.now < TF["15m"]:
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
            "canonical_setup_family": core.canonical_setup_family(a.setup_type), "timeframe": "15m+3m",
            "anchor_id": a.id, "anchor_kind": a.kind, "entry": entry, "limit_price": entry,
            "stop": stop, "initial_stop": stop, "tp": tp1, "tp0": entry+sign*.6*risk,
            "tp1": tp1, "tp2": tp2, "tp3": tp3, "partials": partials,
            "rr1": rr1, "rr2": sign*(tp2-entry)/risk, "rr3": sign*(tp3-entry)/risk,
            "risk": risk, "gross_rr": rr1, "net_rr": net_rr, "costs_r": costs,
            "placement_price": s.price, "target_cancel_armed": sign*(s.price-tp1)<0,
            "score": score, "stop_distance_pct": risk/entry*100,
            "placed_ts": s.now, "expires_ts": expires, "zone_created_ts": a.created_ts,
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
        state["regime_memory"] = copy.deepcopy(ctx.get("regime_memory", {}))
        fresh, detector_audit = [], []
        for detector in core.DETECTORS:
            try:
                a = detector(ctx)
                detector_audit.append({"detector": detector.__name__,"status": "DETECTED" if a else "NO_SETUP"})
                if a is not None:
                    fresh.append(a)
            except Exception as exc:
                detector_audit.append({"detector": detector.__name__,"status": "ERROR","error": type(exc).__name__})
        anchors = merge_full_anchors(fresh,state,s,cfg)
        stats = setup_statistics(state,cfg)
        plans, watch, refused = [], [], {}
        for a in anchors:
            key = anchor_key(a,cfg)
            level_key = ident(a.side,round(a.level/cfg.tick))
            if key in state.get("used_events",{}) or state.get("level_cooldown",{}).get(level_key,0)>s.now:
                p, reason = None, "LEVEL_OR_EVENT_COOLDOWN"
            else:
                p, reason = full_plan(a,ctx,s,cfg,stats)
            if p:
                p["level_key"] = level_key
                plans.append(p)
            else:
                refused[reason] = refused.get(reason,0)+1
                watch.append({"setup": a.setup_type,"anchor_id": a.id,"side": core.side_sign(a.side),
                              "level": a.level,"reason": reason,"score": a.score})
    plans.sort(key=lambda p:(p["score"],p["net_rr"],p["zone_created_ts"]),reverse=True)
    clusters = []
    for p in plans:
        cluster = next((q for q in clusters if q["side"] == p["side"]
                        and abs(q["entry"]-p["entry"]) <= max(3*cfg.tick,.15*ctx["atr3"])),None)
        if cluster is None:
            p["supporting_setups"] = [p["setup"]]
            p["supporting_families"] = [p["canonical_setup_family"]]
            p["parent_keys"] = []
            clusters.append(p)
        else:
            if p["setup"] not in cluster["supporting_setups"]:
                cluster["supporting_setups"].append(p["setup"])
            cluster["parent_keys"].append(p["event_key"])
            if p["canonical_setup_family"] not in cluster["supporting_families"]:
                cluster["supporting_families"].append(p["canonical_setup_family"])
    for p in clusters:
        p["score"] = min(100,p["score"]+min(8,3*(len(p["supporting_families"])-1)))
    clusters.sort(key=lambda p:(p["score"],p["net_rr"]),reverse=True)
    contexts = {tf:{**bias(s.candles[tf]), "side": core.side_sign(core.structure_snapshot(s.candles[tf],60,2)["direction"])}
                for tf in ("15m","1H","4H")}
    return {"health": [],"plans": clusters,"watch": sorted(watch,key=lambda w:-w["score"])[:24],
            "rejections": refused,"contexts": contexts,"full_context": ctx,
            "detectors": detector_audit,"setup_statistics": stats,
            "raw_plans": len(plans),"registered_setups": len(core.DETECTORS)}






def new_state(cfg: Config) -> dict:
    return {"schema": SCHEMA, "version": VERSION, "instrument": cfg.instrument,
            "config_hash": ident(json.dumps(config_dict(cfg), sort_keys=True)),
            "pending": None, "active": None, "used_events": {}, "trades": [],
            "orders": [], "events": [], "signals": [], "last_run_ts": 0,
            "notification_queue": [], "reconciliation": None,
            "anchor_memory": {}, "level_cooldown": {}, "setup_statistics": {}}


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
    fees = p["entry"]*p.get("entry_fee_rate",cfg.maker_fee)/risk+p.get("realized_exit_fees_r",0)+remaining*price*cfg.taker_fee/risk
    closed = {**copy.deepcopy(p), "status": "CLOSED", "exit": price, "closed_ts": ts,
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
    p["realized_exit_fees_r"] = p.get("realized_exit_fees_r",0)+size*exit_price*cfg.taker_fee/p["risk"]
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


def amend_at_report(state: dict, s: Snapshot, cfg: Config) -> None:
    """Decisions happen at the 15m report, never at historical intra-cycle bars."""
    p = state.get("active")
    if not p or p.get("pending_stop") or (p.get("strong_closes", 0) < 2 and not p.get("tp1_hit")):
        return
    entry, side, risk = p["entry"], p["side"], p["risk"]
    if not p.get("tp1_hit") and side*(s.price-entry)/risk < cfg.break_even_trigger_r:
        return
    slip = cfg.slippage_bps/10_000
    be = entry*(side+cfg.maker_fee)/((side-cfg.taker_fee)*(1-side*slip))
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


def advance_lifecycle(state: dict, s: Snapshot, cfg: Config) -> None:
    """Chronological replay before applying CURRENT context. No retroactive cancel."""
    for c in s.candles.get("3m", []):
        if state["active"]:
            manage_bar(state, c, cfg)
            continue
        p = state["pending"]
        if not p or c.ts <= p.get("last_bar", -1):
            continue
        end = c.ts+TF["3m"]
        if c.ts < p["placed_ts"]:
            if end > p["placed_ts"]:
                possible_fill = c.low < p["entry"]-cfg.tick/2 if p["side"] == 1 else c.high > p["entry"]+cfg.tick/2
                if possible_fill:
                    end_order(state, p, "UNRESOLVED", "PLACEMENT_INSIDE_TOUCHED_BAR", end)
            continue
        cancel_ts = p.get("cancel_requested_ts", 0)
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
                     realized_gross_r=0.0,realized_exit_fees_r=0.0,realized_legs=[],entry_version=VERSION)
            # Fill time is interval-censored. Bar OPEN is a conservative age clock.
            p["fill_time_status"] = "WITHIN_3M_BAR"
            p["exit_deadline"] = min(c.ts+cfg.max_hold_minutes*60_000, session_cutoff(c.ts, cfg))
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
        emit(state, "CANCEL_REQUESTED", now, order_id=p["id"], reason=reason)


def run_cycle(state: dict, s: Snapshot, cfg: Config) -> dict:
    if s.now <= state.get("last_run_ts", 0):
        return {"action": "DUPLICATE_OR_OUT_OF_ORDER", "events": [], "analysis": {}}
    if state.get("reconciliation") and not state.get("active"):
        return {"action": "RECONCILIATION_REQUIRED", "events": [], "analysis": {}}
    if state["instrument"] != cfg.instrument:
        raise ValueError("Instrument changed: use a separate state path")
    config_hash = ident(json.dumps(config_dict(cfg), sort_keys=True))
    if state.get("config_hash") != config_hash and (state.get("active") or state.get("pending")):
        raise ValueError("Configuration changed with an open plan; finish its lifecycle first")
    start = len(state["events"])
    # Validate lifecycle before changing ANY state. Missing bars cannot be skipped.
    faults = health(s, cfg)
    lifecycle_faults = execution_health(s, state) if state.get("active") or state.get("pending") else []
    if lifecycle_faults or any(x in faults for x in ("UNTRUSTED_PRICE", "WRONG_INSTRUMENT", "STALE_TICKER", "STALE_3m")):
        return {"action": "DATA_HOLD", "events": [], "analysis": {"health": faults+lifecycle_faults}}
    advance_lifecycle(state, s, cfg)
    amend_at_report(state, s, cfg)
    analysis = analyze(s, cfg, state)
    state["setup_statistics"] = analysis.get("setup_statistics",state.get("setup_statistics",{}))
    ctx = analysis.get("contexts", {})
    pending = state.get("pending")
    if pending and ctx:
        if pending["side"]*(s.price-pending["tp"]) < 0:
            pending["target_cancel_armed"] = True
        opposite = -pending["side"]
        if (cfg.cancel_on_bias_flip and ctx["1H"]["side"] == opposite and ctx["4H"]["side"] == opposite
                and not (pending.get("htf_1h")==opposite and pending.get("htf_4h")==opposite)):
            request_cancel(state, pending, "CONFIRMED_HTF_FLIP", s.now)
        elif pending["side"]*(s.price-pending["invalidation"]) <= 0:
            request_cancel(state, pending, "STRUCTURE_INVALIDATED_NOW", s.now)
        elif pending.get("target_cancel_armed",True) and pending["side"]*(s.price-pending["tp"]) >= 0:
            request_cancel(state, pending, "TARGET_REACHED_WITHOUT_FILL", s.now)
    daily = day_stats(state, s.now, cfg)
    action, selected = "WATCH", None
    if state.get("reconciliation"):
        action = "RECONCILIATION_REQUIRED"
    elif state.get("active"):
        action = "FOLLOW"
    elif state.get("pending"):
        action = "CANCEL_LIMIT" if state["pending"].get("cancel_requested_ts") else "WAIT_LIMIT"
    elif analysis["health"]:
        action = "DATA_HOLD"
    elif s.now >= session_cutoff(s.now, cfg)-TF["15m"]:
        action = "SESSION_CLOSED"
    elif daily["losses_r"] >= cfg.day_loss_cap_r or daily["entries"] >= cfg.max_daily_trades:
        action = "DAILY_PAUSE"
    elif analysis["plans"]:
        top = analysis["plans"][0]
        opposing = next((p for p in analysis["plans"] if p["side"] != top["side"]), None)
        if opposing and top["score"]-opposing["score"] < cfg.conflict_margin:
            action = "DIRECTION_CONFLICT"
        elif top["event_key"] not in state["used_events"] and not any(
                key in state["used_events"] for key in top.get("parent_keys", [])):
            state["pending"] = copy.deepcopy(top)
            selected = top
            action = "PLACE_LIMIT"
            emit(state, "PLACED", s.now, order_id=top["id"], side=top["side"],
                 entry=top["entry"], stop=top["stop"], tp=top["tp"])
    state["last_run_ts"] = s.now
    state["config_hash"] = config_hash
    state["version"] = VERSION
    # Used keys outlive all eligible setups, including cancelled / expired events.
    cutoff = s.now-2*86_400_000
    state["used_events"] = {k: v for k, v in state["used_events"].items() if v >= cutoff}
    state["level_cooldown"] = {k:v for k,v in state.get("level_cooldown",{}).items() if v>s.now}
    summary = {"ts": s.now, "time": iso(s.now), "action": action, "price": s.price,
               "plan_id": selected["id"] if selected else None,
               "rejections": analysis["rejections"] if "rejections" in analysis else {},
               "health": analysis["health"], "daily": daily,
               "detectors": analysis.get("detectors",[]),
               "raw_plans": analysis.get("raw_plans",0),"registered_setups": analysis.get("registered_setups",24)}
    state["signals"].append(summary)
    state["signals"] = state["signals"][-1000:]
    state["watch"] = analysis.get("watch", [])
    return {"action": action, "events": state["events"][start:], "analysis": analysis,
            "selected": selected, "daily": daily}


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


def build_message(state: dict, s: Snapshot, result: dict, cfg: Config) -> str:
    a = result["action"]
    labels = {"PLACE_LIMIT": "НОВИЙ ЛІМІТНИЙ ПЛАН", "WAIT_LIMIT": "ОЧІКУЄМО ЛІМІТ",
              "FOLLOW": "СУПРОВІД", "WATCH": "СПОСТЕРЕЖЕННЯ", "DATA_HOLD": "НЕМАЄ НАДІЙНИХ ДАНИХ",
              "DIRECTION_CONFLICT": "КОНФЛІКТ НАПРЯМКІВ", "DAILY_PAUSE": "ДЕННА ПАУЗА",
              "SESSION_CLOSED": "СЕСІЯ ЗАВЕРШЕНА",
              "RECONCILIATION_REQUIRED": "ПОТРІБНА ЗВІРКА ОРДЕРА",
              "CANCEL_LIMIT": "СКАСУВАТИ НЕВИКОНАНИЙ ЛІМІТ"}
    local = datetime.fromtimestamp(s.now/1000, ZoneInfo(cfg.timezone))
    lines = [f"{cfg.instrument} • {local:%d.%m %H:%M}", labels.get(a, a), f"Ціна: {s.price:.6g}"]
    ctx = result.get("analysis", {}).get("contexts", {})
    names = {1: "вгору", -1: "вниз", 0: "діапазон/перехід"}
    if ctx:
        lines.append("Контекст: "+" • ".join(f"{tf} {names[ctx[tf]['side']]}" for tf in ("4H", "1H", "15m")))
    p = state.get("active") or state.get("pending")
    if p:
        lines += [f"{'LONG' if p['side'] == 1 else 'SHORT'} • {p['setup']} • {p['timeframe']}",
                  f"Ліміт: {p['entry']:.6g}", f"SL: {p.get('pending_stop', {}).get('price', p['stop']):.6g} • TP: {p['tp']:.6g}",
                  f"Стоп від входу: {p['stop_distance_pct']:.2f}% • чистий R:R плану: {p['net_rr']:.2f}",
                  f"Умова: повернення до зони; інвалідація {p['invalidation']:.6g}"]
        if state.get("pending"):
            end = datetime.fromtimestamp(p["expires_ts"]/1000, ZoneInfo(cfg.timezone))
            lines.append(f"Скасувати невиконаний ордер о {end:%H:%M} або за сигналом скасування.")
            if p.get("cancel_requested_ts"):
                lines.append(f"СКАСУВАТИ ЗАРАЗ: {p['cancel_reason']}; звірити, чи не виконаний.")
        else:
            end = datetime.fromtimestamp(p["exit_deadline"]/1000, ZoneInfo(cfg.timezone))
            lines.append(f"Завершити угоду до {end:%H:%M}; SL/TP мають діяти між повідомленнями.")
        if p.get("tp2"):
            lines.append(f"Цілі: TP1 {p['tp1']:.6g} • TP2 {p['tp2']:.6g} • TP3 {p['tp3']:.6g}")
        if p.get("supporting_setups"):
            lines.append("Узгоджені сетапи: "+", ".join(p["supporting_setups"]))
        lines.append("Підстава: "+"; ".join(e for e in p["evidence"][:3] if not e.startswith("parent:")))
        lines.append(f"Оцінка умов: {p['score']:.0f}/100; імовірність успіху ще не валідована.")
    for e in result.get("events", []):
        if e["kind"] == "CLOSED":
            lines.append(f"PAPER закриття: {e['reason']} • {e['net_r']:+.2f}R")
        elif e["kind"] in ("EXPIRED", "CANCELLED", "UNRESOLVED"):
            lines.append(f"Ордер {e['order_id']}: {e['kind']} ({e['reason']})")
        elif e["kind"] == "FILLED":
            lines.append("PAPER: ліміт перетнуто ціною; фактичне виконання перевірте на біржі.")
        elif e["kind"] == "PAPER_STOP_AMENDED":
            lines.append(f"PAPER SL змінено: {e['stop']:.6g}; перевірте фактичний ордер.")
        elif e["kind"] in {"TP1","TP2","TP3"}:
            lines.append(f"PAPER {e['kind']}: {e['price']:.6g} • зафіксовано {e['fraction']*100:.0f}% позиції")
    errors = result.get("analysis", {}).get("health", [])
    if state.get("reconciliation"):
        r = state["reconciliation"]
        lines.append(f"Ордер {r['order_id']}: {r['reason']}. Порядок подій у свічці невідомий.")
        lines.append("Нові плани призупинено; звірте виконання/скасування на біржі.")
    if errors:
        lines.append("Причина: "+", ".join(errors))
    rejections = result.get("analysis", {}).get("rejections", {})
    if not p and rejections:
        lines.append("Відхилення: "+", ".join(f"{k} ({v})" for k, v in sorted(rejections.items(), key=lambda x:-x[1])[:3]))
    if not p and not rejections and not errors:
        lines.append("Усі 24 сетапи перевірено; чекаємо узгоджений лімітний сценарій.")
    detectors = result.get("analysis",{}).get("detectors",[])
    if detectors:
        lines.append(f"Сетапи: {len(detectors)}/24 перевірено • помилки: {sum(d['status']=='ERROR' for d in detectors)}")
    lines.append("PAPER/сигнали • 3m/15m/1h/4h • звіт кожні 15 хв • виконання на біржі не підключене")
    return "\n".join(lines)[:3900]


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
        return migrate_state(raw,cfg)
    for key in ("trades", "orders", "events", "signals", "notification_queue", "used_events"):
        if key not in raw:
            raise ValueError(f"State missing {key}; refusing reset")
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
        try:
            data = http_json("https://api.telegram.org/bot"+token+"/sendMessage",
                             {"chat_id": chat_id, "text": item["text"]})
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
                p['realized_exit_fees_r'] += fraction*level*cfg.taker_fee/risk
        if 'LIMIT' not in p['execution_source']:
            p['entry_fee_rate'] = cfg.taker_fee
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
        read_json(journal_path)
        if ack and state.get('reconciliation'):
            emit(state,'RECONCILIATION_ACKNOWLEDGED',int(time.time()*1000),
                 order_id=state['reconciliation']['order_id'])
            state['reconciliation'] = None
            state['legacy_pending_orders'] = []
        snap = Snapshot.parse(read_json(snapshot_path)) if snapshot_path else collect_snapshot(cfg,state['last_run_ts'])
        result = run_cycle(state,snap,cfg)
        message = build_message(state,snap,result,cfg)
        print(message)
        if result['action']=='DUPLICATE_OR_OUT_OF_ORDER':
            export_journal(journal_path,state,core)
            return 0 if not notify or notify_queue(state,core.TELEGRAM_TOKEN,core.TELEGRAM_CHAT_ID,state_path) else 2
        if notify:
            state['notification_queue'].append({'id':ident(snap.now,message),'text':message})
        atomic_write(state_path,state)
        export_journal(journal_path,state,core)
        if notify and not notify_queue(state,core.TELEGRAM_TOKEN,core.TELEGRAM_CHAT_ID,state_path):
            return 2
        return 1 if result['action']=='DATA_HOLD' else 0


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
        suite = unittest.defaultTestLoader.discover(str(ROOT),pattern='test_full_limit_engine.py')
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
