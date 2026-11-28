#!/usr/bin/env python3
"""BZU Multi-Asset Institutional Scanner for MEXC Futures (Oil + Platinum + Crypto).

Scans UKOIL_USDT, XPT_USDT (Platinum), SOL_USDT, LINK_USDT, LTC_USDT, APT_USDT,
and ORDI_USDT using MEXC public futures REST API (no API keys required), applies
independent LONG/SHORT local transitions, BTC as soft relative-strength context,
three cluster slots with a shared risk and notional budget, structural ATR geometry,
and sends deduplicated Ukrainian Telegram alerts for A+ setups.
"""
from __future__ import annotations

import argparse
import logging
import fcntl
from concurrent.futures import ThreadPoolExecutor, as_completed
from mexc_hybrid import detect, trend_efficiency
from mexc_opportunity import MODEL_SCHEMA, PREVIOUS_SCHEMA, market_state, beta_relative, rates, cost_r, estimate, objective, policy, shadow_step, register, SOFT, break_memory, memory_retest, funding_accrual
from mexc_lifecycle import advance
from mexc_evidence import SHADOW_FAMILIES, active_estimate, exit_policies, tag_experiment
import mexc_h1_trend
import mexc_portfolio_intelligence as portfolio_intel
import mexc_trend_research as trend_research
import mexc_geometry_research
from copy import deepcopy
from mexc_direction import (context as direction_context, relative_strength, detect_transitions,
    closed_prefix, contiguous, trigger_age_limit, signal_event_key, delayed_guard)
from mexc_geometry import geometry, POLICY
from threading import Lock
import hashlib
import json
import math
from decimal import Decimal
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

MEXC_BASE = (os.getenv("MEXC_FUTURES_BASE") or "https://api.mexc.com").rstrip("/")
KYIV_TZ = ZoneInfo("Europe/Kyiv")
SCANNER_VERSION = "BZU MULTI v16.0 Trend Evidence & Alpha Research"
SHADOW_IN_ACTIVE_HISTORY = False

FAMILY_RISK_MULT = {
    "FAILED_BREAKOUT": 1.25,
    "LIQUIDITY_SWEEP": 1.15,
    "BREAKOUT_RETEST": 1.15,
    "EXHAUSTION": 1.00,
    "TREND_PULLBACK": 0.75,
    "MOMENTUM": 0.30,
    "EARLY_STRUCTURE_SHIFT": 0.65,
    "COUNTERTREND_WAVE": 0.55,
    "STRUCTURE_CONTINUATION": 0.75,
}

CLOSE_REASON_UA = {
    "TAKE_PROFIT": "Тейк-профіт (TP)",
    "TP1_RUNNER_TP": "TP1 (50%) + Runner TP",
    "TP1_BE_STOP": "TP1 (50%) + Б/У залишку",
    "BE_STOP": "Беззбиток / Структурний трейлінг",
    "STOP": "Стоп-лосс",
    "THESIS_DECAY": "Згасання торгової ідеї",
    "STALE_EXIT": "Дострокове закриття (Тайм-стоп Alpha Decay)",
    "TIME_EXIT": "Закриття за лімітом часу (18 год)",
    "LIMIT_EXPIRED": "Лімітка скасована (таймаут)",
    "LIMIT_MISSED_TARGET": "Ціна дійшла до TP без заповнення лімітки",
}

def close_reason_ua(reason: str | None) -> str:
    return CLOSE_REASON_UA.get(str(reason or ""), str(reason or "—"))

INSTRUMENTS: tuple[dict[str, Any], ...] = (
    {
        "symbol": "UKOIL_USDT",
        "label": "UKOIL (Brent)",
        "short": "UKOIL",
        "cluster": "ENERGY",
        "cluster_ua": "НАФТА",
        "fee_tag": "0% Maker / 0.02%",
        "min_stop_pct": 0.0035,
        "max_stop_pct": 0.0095,
        "atr_mult": 1.35,
        "min_rvol": 1.15,
        "min_rr": 1.75,
        "max_rr": 2.25,
        "use_btc_filter": False,
        "mexc_url": "https://www.mexc.com/futures/UKOIL_USDT",
    },
    {
        "symbol": "XPT_USDT",
        "label": "PLATINUM (XPT)",
        "short": "XPT",
        "cluster": "METALS",
        "cluster_ua": "МЕТАЛ",
        "fee_tag": "0% FEE",
        "min_stop_pct": 0.0035,
        "max_stop_pct": 0.0150,
        "atr_mult": 1.40,
        "min_rvol": 1.20,
        "min_rr": 1.75,
        "max_rr": 2.15,
        "use_btc_filter": False,
        "mexc_url": "https://www.mexc.com/futures/XPT_USDT",
    },
    {
        "symbol": "SOL_USDT",
        "label": "Solana (SOL)",
        "short": "SOL",
        "cluster": "CRYPTO",
        "cluster_ua": "КРИПТА",
        "fee_tag": "0% FEE",
        "min_stop_pct": 0.0045,
        "max_stop_pct": 0.0200,
        "atr_mult": 1.45,
        "min_rvol": 1.20,
        "min_rr": 1.80,
        "max_rr": 2.25,
        "use_btc_filter": True,
        "mexc_url": "https://www.mexc.com/futures/SOL_USDT",
    },
    {
        "symbol": "LINK_USDT",
        "label": "Chainlink (LINK)",
        "short": "LINK",
        "cluster": "CRYPTO",
        "cluster_ua": "КРИПТА",
        "fee_tag": "0% Maker / 0.02%",
        "min_stop_pct": 0.0045,
        "max_stop_pct": 0.0200,
        "atr_mult": 1.45,
        "min_rvol": 1.20,
        "min_rr": 1.80,
        "max_rr": 2.25,
        "use_btc_filter": True,
        "mexc_url": "https://www.mexc.com/futures/LINK_USDT",
    },
    {
        "symbol": "LTC_USDT",
        "label": "Litecoin (LTC)",
        "short": "LTC",
        "cluster": "CRYPTO",
        "cluster_ua": "КРИПТА",
        "fee_tag": "0% Maker / 0.02%",
        "min_stop_pct": 0.0040,
        "max_stop_pct": 0.0180,
        "atr_mult": 1.40,
        "min_rvol": 1.20,
        "min_rr": 1.80,
        "max_rr": 2.20,
        "use_btc_filter": True,
        "mexc_url": "https://www.mexc.com/futures/LTC_USDT",
    },
    {
        "symbol": "APT_USDT",
        "label": "Aptos (APT)",
        "short": "APT",
        "cluster": "CRYPTO",
        "cluster_ua": "КРИПТА",
        "fee_tag": "0% Maker / 0.02%",
        "min_stop_pct": 0.0055,
        "max_stop_pct": 0.0240,
        "atr_mult": 1.55,
        "min_rvol": 1.35,
        "min_rr": 1.85,
        "max_rr": 2.30,
        "use_btc_filter": True,
        "mexc_url": "https://www.mexc.com/futures/APT_USDT",
    },
    {
        "symbol": "ORDI_USDT",
        "label": "ORDI",
        "short": "ORDI",
        "cluster": "CRYPTO",
        "cluster_ua": "КРИПТА",
        "fee_tag": "0% Maker / 0.02%",
        "min_stop_pct": 0.0055,
        "max_stop_pct": 0.0240,
        "atr_mult": 1.55,
        "min_rvol": 1.35,
        "min_rr": 1.85,
        "max_rr": 2.30,
        "use_btc_filter": True,
        "mexc_url": "https://www.mexc.com/futures/ORDI_USDT",
    },
)

TF_SECONDS = {
    "Min1": 60,
    "Min5": 300,
    "Min15": 900,
    "Min60": 3600,
    "Hour4": 14400,
}


@dataclass(frozen=True)
class Bar:
    ts: int  # milliseconds
    open: float
    high: float
    low: float
    close: float
    volume: float


_request_lock = Lock()
_next_request = 0.0


def _http_json(url: str, timeout: float = 7.0) -> dict[str, Any]:
    global _next_request
    # MEXC kline limit: 20 / 2 s. Pace requests, including retries.
    with _request_lock:
        time.sleep(max(0., _next_request-time.monotonic()))
        _next_request=time.monotonic()+.14
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "BZU-MultiScanner/15.4", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload=json.loads(resp.read().decode("utf-8"))
        if isinstance(payload,dict) and payload.get("success") is False:
            raise RuntimeError(f"MEXC API code {payload.get('code','unknown')}")
        return payload


def fetch_all_tickers(fetcher=_http_json) -> dict[str, dict[str, Any]]:
    payload = fetcher(f"{MEXC_BASE}/api/v1/contract/ticker")
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        sym = row.get("symbol")
        if sym:
            out[sym] = row
    return out


def fetch_contract_ticks(fetcher=_http_json, contract_specs=None) -> dict[str, float]:
    payload = fetcher(f"{MEXC_BASE}/api/v1/contract/detail")
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return {}
    out: dict[str, float] = {}
    for row in rows:
        sym = row.get("symbol")
        unit = row.get("priceUnit")
        if sym and contract_specs is not None:
            spec=portfolio_intel.contract_spec(row)
            if spec is not None: contract_specs[sym]=spec
        if sym and unit:
            try:
                val = float(unit)
                if math.isfinite(val) and val > 0:
                    out[sym] = val
            except (TypeError, ValueError):
                pass
    return out


def parse_mexc_klines(payload: dict[str, Any], step_sec: int, now_ms: int) -> list[Bar]:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return []
    times = data.get("time") or []
    opens = data.get("open") or []
    highs = data.get("high") or []
    lows = data.get("low") or []
    closes = data.get("close") or []
    vols = data.get("vol") or []
    n = min(len(times), len(opens), len(highs), len(lows), len(closes), len(vols))
    step_ms = step_sec * 1000
    bars: list[Bar] = []
    for i in range(n):
        try:
            t_raw = int(times[i])
            ts = t_raw * 1000 if t_raw < 10_000_000_000 else t_raw
            # Keep only closed bars
            if ts + step_ms > now_ms:
                continue
            o, h, l, c, v = (
                float(opens[i]),
                float(highs[i]),
                float(lows[i]),
                float(closes[i]),
                float(vols[i]),
            )
            if all(math.isfinite(x) for x in (o, h, l, c, v)) and l > 0 and l <= min(o,c) <= max(o,c) <= h:
                bars.append(Bar(ts=ts, open=o, high=h, low=l, close=c, volume=max(0.0, v)))
        except (TypeError, ValueError):
            continue
    return sorted({b.ts:b for b in bars}.values(), key=lambda b: b.ts)


def fetch_klines(symbol: str, interval: str, bars_needed: int, now_ms: int, fetcher=_http_json) -> list[Bar]:
    step = TF_SECONDS[interval]
    end_sec = now_ms // 1000
    start_sec = end_sec - (bars_needed + 10) * step
    url = f"{MEXC_BASE}/api/v1/contract/kline/{urllib.parse.quote(symbol)}?interval={interval}&start={start_sec}&end={end_sec}"
    payload = fetcher(url)
    return parse_mexc_klines(payload, step, now_ms)


def aggregate_bars(rows, source_sec, target_sec, now_ms):
    step=source_sec*1000; span=target_sec*1000
    if span%step: raise ValueError("incompatible intervals")
    buckets={}
    for b in rows:
        bucket=b.ts//span*span
        if bucket+span<=now_ms: buckets.setdefault(bucket,{})[b.ts]=b
    out=[]
    for bucket,parts in sorted(buckets.items()):
        times=list(range(bucket,bucket+span,step))
        if not all(t in parts for t in times): continue
        q=[parts[t] for t in times]
        out.append(Bar(bucket,q[0].open,max(x.high for x in q),min(x.low for x in q),q[-1].close,sum(x.volume for x in q)))
    return out


def ema(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    val = sum(values[:period]) / period
    for x in values[period:]:
        val = x * k + val * (1.0 - k)
    return val


def atr(bars: list[Bar], period: int = 14) -> float:
    if len(bars) < 2:
        return 0.0
    trs: list[float] = []
    for prev, cur in zip(bars, bars[1:]):
        tr = max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close))
        trs.append(tr)
    window = trs[-period:]
    return sum(window) / len(window) if window else 0.0


def session_vwap(bars_15m: list[Bar]) -> float | None:
    if not bars_15m:
        return None
    recent = bars_15m[-96:]  # last 24h
    num = sum(((b.high + b.low + b.close) / 3.0) * max(b.volume, 1e-9) for b in recent)
    den = sum(max(b.volume, 1e-9) for b in recent)
    return num / den if den > 0 else None


def tf_bias(bars: list[Bar]) -> int:
    if len(bars) < 22:
        return 0
    closes = [b.close for b in bars]
    e9 = ema(closes, 9)
    e21 = ema(closes, 21)
    if e9 is None or e21 is None:
        return 0
    last = closes[-1]
    if last > e9 > e21:
        return 1
    if last < e9 < e21:
        return -1
    return 0


def round_price(price: float, tick: float) -> float:
    if tick <= 0:
        return round(price, 4)
    decimals = max(0, min(12, -Decimal(str(tick)).normalize().as_tuple().exponent))
    return round(round(price / tick) * tick, decimals)


def format_price(price: float | None, tick: float) -> str:
    if price is None or not math.isfinite(price):
        return "—"
    decimals = max(0, min(12, -Decimal(str(tick)).normalize().as_tuple().exponent)) if tick > 0 else 2
    return f"{price:.{decimals}f}"


def compute_sizing(
    entry: float,
    stop: float,
    tp: float,
    *,
    deposit: float = 100.0,
    risk_pct: float = 3.0,
    max_leverage: float = 5.0,
) -> dict[str, float]:
    sl_dist = abs(entry - stop)
    tp_dist = abs(tp - entry)
    sl_pct = sl_dist / entry if entry > 0 else 0.0
    tp_pct = tp_dist / entry if entry > 0 else 0.0
    target_risk_usd = deposit * (risk_pct / 100.0)
    max_notional = deposit * max_leverage
    raw_notional = (target_risk_usd / sl_pct) if sl_pct > 1e-9 else max_notional
    notional = min(max_notional, raw_notional)
    eff_lev = notional / deposit if deposit > 0 else 0.0
    actual_risk = notional * sl_pct
    expected_profit = notional * tp_pct
    return {
        "deposit_usdt": round(deposit, 2),
        "risk_pct": round(risk_pct, 2),
        "max_leverage": round(max_leverage, 2),
        "position_usdt": round(notional, 2),
        "effective_leverage": round(eff_lev, 2),
        "risk_usd": round(actual_risk, 2),
        "target_profit_usd": round(expected_profit, 2),
        "sl_pct": round(sl_pct * 100.0, 2),
        "tp_pct": round(tp_pct * 100.0, 2),
    }


def cost_rates():
    fee=float(os.getenv("MEXC_TAKER_FEE_RATE","0.0002"))
    slip=float(os.getenv("MEXC_SLIPPAGE_RATE","0.0001"))
    if not (math.isfinite(fee) and math.isfinite(slip) and 0<=fee<=.02 and 0<=slip<=.02):
        raise ValueError("Invalid MEXC paper cost assumptions")
    return fee,slip


def analyze_btc_macro(bars_15m: list[Bar], bars_1h: list[Bar], price: float) -> dict[str, Any]:
    b15 = tf_bias(bars_15m)
    b1h = tf_bias(bars_1h)
    vwap = session_vwap(bars_15m)
    above_vwap = (price >= vwap) if (vwap and price > 0) else None
    if b1h == 1 and (b15 == 1 or above_vwap is True):
        state, side, label_ua = "BULLISH", 1, "Бичачий ↑"
    elif b1h == -1 and (b15 == -1 or above_vwap is False):
        state, side, label_ua = "BEARISH", -1, "Ведмежий ↓"
    else:
        state, side, label_ua = "NEUTRAL", 0, "Нейтральний ↔"
    return {
        "symbol": "BTC_USDT",
        "price": price,
        "bias_1h": b1h,
        "bias_15m": b15,
        "vwap": round(vwap, 2) if vwap else None,
        "state": state,
        "side": side,
        "label_ua": label_ua,
    }


def _detect_fvg(bars: list[Bar], side: int) -> tuple[float, float] | None:
    for i in range(len(bars) - 1, max(2, len(bars) - 12), -1):
        c0, c2 = bars[i - 2], bars[i]
        if side == 1 and c2.low > c0.high:
            return (c0.high, c2.low)
        if side == -1 and c2.high < c0.low:
            return (c2.high, c0.low)
    return None


def analyze_instrument(
    spec: dict[str, Any],
    *,
    ticker: dict[str, Any],
    bars_5m: list[Bar],
    bars_15m: list[Bar],
    bars_1h: list[Bar],
    bars_4h: list[Bar],
    bars_1m: list[Bar] | None = None,
    history: list[dict[str, Any]] | None = None,
    btc_bars_15m: list[Bar] | None = None,
    consumed_signals: dict[str, int] | None = None,
    prior_breaks: list[dict] | None = None,
    tick: float,
    btc_macro: dict[str, Any],
    now_ms: int,
    deposit: float = 100.0,
    risk_pct: float = 3.0,
    max_leverage: float = 5.0,
) -> dict[str, Any]:
    history=[r for r in (history or []) if r.get("source","PAPER")=="PAPER" and isinstance(r.get("closed_ts"),int) and r["closed_ts"]<now_ms]
    bars_1m = closed_prefix(bars_1m or [],60000,now_ms)
    bars_5m = closed_prefix(bars_5m,300000,now_ms)
    bars_15m = closed_prefix(bars_15m,900000,now_ms)
    bars_1h = closed_prefix(bars_1h,3600000,now_ms)
    bars_4h = closed_prefix(bars_4h,14400000,now_ms)
    btc_bars_15m = closed_prefix(btc_bars_15m or [],900000,now_ms)
    price = float(ticker.get("lastPrice") or (bars_5m[-1].close if bars_5m else 0.0))
    bid = float(ticker.get("bid1") or price)
    ask = float(ticker.get("ask1") or price)
    change24h = float(ticker.get("riseFallRate") or 0.0) * 100.0
    vol24h = float(ticker.get("volume24") or 0.0)
    funding_rate = float(ticker.get("fundingRate") or 0.0)
    open_interest = float(ticker.get("holdVol") or 0.0)
    funding_pct = round(funding_rate * 100.0, 4)

    base_out = {
        "symbol": spec["symbol"],
        "label": spec["label"],
        "short": spec["short"],
        "cluster": spec["cluster"],
        "cluster_ua": spec["cluster_ua"],
        "fee_tag": "Оцінені maker/taker витрати",
        "mexc_url": spec["mexc_url"],
        "price": round_price(price, tick),
        "tick": tick,
        "change_24h_pct": round(change24h, 2),
        "volume_24h": round(vol24h, 2),
        "funding_rate": funding_rate,
        "funding_pct": funding_pct,
        "open_interest": round(open_interest, 2),
        "funding_regime": "NEUTRAL",
        "bias_4h": 0,
        "bias_1h": 0,
        "bias_15m": 0,
        "status": "WAIT",
        "status_ua": "НЕМАЄ СИГНАЛУ",
        "score": 0,
        "side": 0,
        "side_ua": "НЕЙТРАЛЬНО",
        "setup": None,
        "setup_ua": "Немає підтвердженого сетапу",
        "order_type": None,
        "entry": None,
        "stop": None,
        "tp": None,
        "rr": None,
        "rvol": None,
        "range_pos": None,
        "btc_aligned": True,
        "reason_ua": "Недостатньо свічок для аналізу",
        "sizing": None,
        "cluster_locked": False,
    }

    # Preserve exact execution OHLC (never round the high/low across a stop).
    def pack(rows, limit=None):
        return [{"t": b.ts//1000, "o": b.open, "h": b.high, "l": b.low, "c": b.close, "v": b.volume} for b in (rows[-limit:] if limit else rows)]
    base_out.update(candles_15m=pack(bars_15m,80), candles_5m=pack(bars_5m,80),
                    candles_1m=pack(bars_1m or []), execution_tf="1m", bid=bid, ask=ask,
                    quote_valid=bool(ticker.get("lastPrice") and ticker.get("bid1") and ticker.get("ask1") and 0<bid<=ask),
                    quote_ts=int(ticker.get("_observed_ts",now_ms)))

    if price <= 0 or len(bars_5m) < 25 or len(bars_15m) < 30 or len(bars_1h) < 25:
        base_out.update(status="WATCH",status_ua="ДАНІ НЕСВІЖІ",reason_ua="Недостатньо підтверджених свічок; сигнал не оцінено")
        return base_out

    b4 = tf_bias(bars_4h)
    b1 = tf_bias(bars_1h)
    b15 = tf_bias(bars_15m)
    a15 = atr(bars_15m, 14)
    a5 = atr(bars_5m, 14)
    recent_24h = bars_15m[-96:]
    r_low = min(b.low for b in recent_24h)
    r_high = max(b.high for b in recent_24h)
    range_pos = (price - r_low) / (r_high - r_low) if r_high > r_low else 0.5

    last5 = bars_5m[-1]
    prev5_slice = bars_5m[-16:-1]
    avg_vol5 = sum(b.volume for b in prev5_slice) / max(1, len(prev5_slice))
    rvol5 = (last5.volume / avg_vol5) if avg_vol5 > 0 else 1.0

    base_out.update(
        bias_4h=b4,
        bias_1h=b1,
        bias_15m=b15,
        rvol=round(rvol5, 2),
        range_pos=round(range_pos, 3),
    )

    # Freshness is based on closed bars; never turn a stale signal into a MARKET entry.
    fresh = all(rows and 0 <= now_ms-(rows[-1].ts+step) <= step*2 for rows,step in
                ((bars_5m,300000),(bars_15m,900000),(bars_1h,3600000),(bars_4h,14400000)))
    if not fresh or not base_out["quote_valid"] or not 0<=now_ms-base_out["quote_ts"]<=60000 or not bars_1m or not 0<=now_ms-bars_1m[-1].ts-60000<=120000:
        base_out.update(status="WATCH",status_ua="ДАНІ НЕСВІЖІ",reason_ua="Немає свіжих свічок або виконуваної котировки")
        return base_out
    base_out["analysis_fresh"]=True
    t1,e1=trend_efficiency(bars_1h); t4,e4=trend_efficiency(bars_4h,8)
    htf_regime="TREND_UP" if t1==1 and t4!=-1 else "TREND_DOWN" if t1==-1 and t4!=1 else "RANGE_TRANSITION"
    local=direction_context(bars_5m,bars_15m)
    relative=beta_relative(bars_15m,btc_bars_15m,relative_strength(bars_15m,btc_bars_15m)) if spec["use_btc_filter"] else {"available":False,"side":0}
    wave_side=local["side"]
    htf_side=t1 or b1 or b4
    regime=("TRANSITION_UP" if wave_side==1 else "TRANSITION_DOWN") if wave_side and htf_side==-wave_side else ("LOCAL_UP" if wave_side==1 else "LOCAL_DOWN") if wave_side and htf_regime=="RANGE_TRANSITION" else htf_regime
    ms=market_state(bars_5m,bars_15m,local,htf_side,ask-bid)
    base_out["opportunity_breaks"]=break_memory(bars_5m,prior_breaks,now_ms,tick)
    base_out.update(market_state=ms,htf_regime=htf_regime,regime=regime,local_wave=local,relative_strength=relative,
                    wave_side=wave_side,observed_wave_side=wave_side or local["m15"]["side"],model_schema=MODEL_SCHEMA)
    candidates=[]
    diagnostics={str(side):{"side":side,"local_support":wave_side==side,"windows_checked":[],"detected":0,"ready":0,"rejections":[]} for side in (1,-1)}
    allow_m1 = os.getenv("BZU_ALLOW_M1_ENTRY", "true").lower() in ("1", "true", "yes")
    windows=[("M1",bars_1m[:-offset] if offset else bars_1m,60000,offset) for offset in (0,1,2)] if allow_m1 else []
    windows += [("M5",bars_5m[:-offset] if offset else bars_5m,300000,offset) for offset in (0,1)]
    for side in (1,-1):
        diag=diagnostics[str(side)]
        for tf, rows, step, offset in windows:
            if not rows or not 0<=now_ms-(rows[-1].ts+step)<trigger_age_limit(tf) or not contiguous(rows,step,22):
                continue
            known=rows[-1].ts+step
            # Offset is calendar-relative, not merely array-relative (missing latest bar).
            offset=(now_ms-known)//step
            diag["windows_checked"].append({"tf":tf,"offset":offset,"known_ts":known})
            # Trigger evidence and zones are evaluated as they existed when the bar CLOSED.
            m5_at=closed_prefix(bars_5m,300000,known)
            m15_at=closed_prefix(bars_15m,900000,known)
            h1_at=closed_prefix(bars_1h,3600000,known)
            h4_at=closed_prefix(bars_4h,14400000,known)
            if len(m5_at)<22 or len(m15_at)<25 or len(h1_at)<24: continue
            local_at=direction_context(m5_at,m15_at)
            b1_at,b4_at,b15_at=tf_bias(h1_at),tf_bias(h4_at),tf_bias(m15_at)
            t1_at,_=trend_efficiency(h1_at)
            t4_at,_=trend_efficiency(h4_at,8)
            aligned=b1_at==side and b4_at==side
            opposing=b1_at==-side and b4_at==-side
            higher_opposes=any(x==-side for x in (b1_at,b4_at,t1_at,t4_at))
            local_support=local_at["side"]==side
            trend=((b1_at==side or t1_at==side) and not opposing) or local_support
            btc_side=btc_macro.get("side",0)  # current macro is a ranking factor, never a trigger
            btc_opposes=bool(spec["use_btc_filter"] and btc_side==-side)
            rel_support=bool(relative.get("available") and relative.get("side")==side)
            fvg=_detect_fvg(m15_at,side)
            vwap_at=session_vwap(m15_at)
            zones=[x for x in (ema([b.close for b in m15_at],20),vwap_at,(sum(fvg)/2 if fvg else None)) if x is not None]
            aa=atr(rows[:-1],14)
            if aa>0:
                prior=rows[-7:-1]; c=rows[-1]; level=max(b.high for b in prior) if side==1 else min(b.low for b in prior)
                vol=sum(b.volume for b in rows[-21:-1])/20
                diag["windows_checked"][-1].update(body_atr=round(side*(c.close-c.open)/aa,3),
                    break_distance_atr=round(side*(c.close-level)/aa,3),rvol=round(c.volume/vol,3) if vol else None,
                    local_support=local_support,higher_opposes=higher_opposes)
            zone=min(zones,key=lambda x:abs(x-rows[-1].close)) if zones else None
            if zone is not None and abs(zone-rows[-1].close)>.65*atr(m15_at): zone=None
            args=dict(side=side,atr=aa,tick=tick,min_rvol=spec["min_rvol"],trend=trend,zone=zone,timeframe=tf)
            theses=detect(rows,**args,retest_window=4)
            theses+=detect_transitions(rows,side=side,atr=aa,tick=tick,min_rvol=spec["min_rvol"],context_now=local_at,higher_opposes=higher_opposes,timeframe=tf)
            theses=[tag_experiment(t) for t in theses]
            # Keep long-window and memory variants independent; never replace the active short retest.
            short=[t for t in theses if t["family"]=="BREAKOUT_RETEST"]
            for t in detect(rows,**args,retest_window=12):
                if t["family"]=="BREAKOUT_RETEST" and not any(t.get("retest_level")==x.get("retest_level") and t.get("structural_stop")==x.get("structural_stop") for x in short):
                    theses.append({**t,"research_variant":"RETEST_12"})
            if tf=="M5":
                theses += [{**t,"research_variant":"MEMORY_RETEST"} for t in memory_retest(rows,break_memory(rows,prior_breaks,known,tick),side,aa,tick,tf)]
            theses=list({(t["family"],t.get("research_variant")):t for t in theses}.values())
            for thesis in theses:
                reversal=thesis["family"] in ("LIQUIDITY_SWEEP","FAILED_BREAKOUT","EXHAUSTION")
                transition=bool(thesis.get("transition_evidence"))
                diag["detected"]+=1
                # Non-blocking adaptive alignment tier (keeps early reversals alive!)
                if aligned:
                    align_tier, risk_mult, adapt_trail_r = "ALIGNED", 1.0, 1.25
                elif (b4_at==side or b1_at==side) and b4_at!=-side:
                    align_tier, risk_mult, adapt_trail_r = "EARLY_PARTIAL", 0.35, 1.25
                else:
                    align_tier, risk_mult, adapt_trail_r = "EARLY_REVERSAL", 0.50, 1.25

                # Non-blocking WR boosters from proven v14.4 setups:
                # (1) Discount/Premium location preference, (2) VWAP/Sweep reclaim bonus, (3) Consolidation decay
                in_edge = (range_pos <= 0.35) if side == 1 else (range_pos >= 0.65)
                in_mid = 0.42 <= range_pos <= 0.58
                vwap_reclaim = bool(vwap_at and side * (rows[-1].close - vwap_at) > 0 and side * (rows[-2].open - vwap_at) <= 0)
                churn_bars = sum(1 for b in m5_at[-12:] if abs(b.close - rows[-1].close) <= max(2 * tick, 0.35 * a5)) if a5 > 0 else 0

                score=thesis["base_score"] + (8 if aligned else 3 if trend else -4 if opposing else 0)
                score+=4 if b15_at==side else 0
                score+=3 if thesis["rvol"]>=spec["min_rvol"]+.25 else 0
                score+=5 if local_support else 0
                score+=3 if transition else 0
                score+=(-3 if btc_opposes and rel_support else -6 if btc_opposes else 2 if spec["use_btc_filter"] and btc_side==side else 0)
                score+=(4 if rel_support else -3 if relative.get("available") and relative.get("side")==-side else 0)
                score+=2 if tf=="M5" else 0
                score+=4 if (thesis["family"]=="LIQUIDITY_SWEEP" or vwap_reclaim) else 0
                score+=3 if in_edge else (-4 if in_mid else 0)
                score-=5 if churn_bars >= 10 and thesis["family"] not in ("MOMENTUM","BREAKOUT_RETEST","STRUCTURE_CONTINUATION") else 0
                score-=min(3,int(max(0,now_ms-thesis["trigger_ts"]-step)//60000))
                # Non-blocking Funding Rate + Open Interest conviction (MEXC perpetuals microstructure)
                side_fund = side * funding_rate
                if side_fund >= 0.0004:
                    funding_regime, funding_risk_mult, tp1_r, runner_boost = "CROWDED_WITH_US", 0.75, 0.85, 1.0
                    early_close_hint_ua = f"⚠ Перегрітий Funding ({funding_pct:+.3f}%) · ризик ×0.75 · TP1 лише в Shadow"
                elif side_fund <= -0.0002:
                    funding_regime, funding_risk_mult, tp1_r, runner_boost = "SQUEEZE_TAILWIND", 1.0, 1.0, 1.15
                    score += 3
                    early_close_hint_ua = f"🔥 Контр-Funding ({funding_pct:+.3f}%) · потенційний сквіз · розширений Runner TP"
                else:
                    funding_regime, funding_risk_mult, tp1_r, runner_boost = "NEUTRAL", 1.0, 1.0, 1.0
                    early_close_hint_ua = None
                fam_mult = FAMILY_RISK_MULT.get(thesis["family"], 1.0)
                total_risk_mult = round(max(0.20, min(1.35, risk_mult * fam_mult * funding_risk_mult)), 3)

                score=max(0,min(99,score))
                # A confirmed reversal can compete against HTF; no blanket BTC/HTF veto.
                quote=ask if side==1 else bid
                entry=round_price(quote,tick)
                ref=thesis["reference"]
                structural=thesis["structural_stop"]
                # Do not shrink the stop INSIDE invalidation. No chasing or opposite V-candle.
                last_m1=(bars_1m or bars_5m)[-1]
                adverse=side*(last_m1.close-last_m1.open)<-.8*max(atr((bars_1m or bars_5m)[:-1]),tick) and side*(last_m1.close-ref)<0
                route="MARKET"
                maker_pullback=False
                limit_ttl_min=8
                costs=rates(spec["symbol"],"MARKET")
                fee=(costs["entry_fee_rate"]+costs["exit_fee_rate"])/2
                slip=(costs["entry_slip_rate"]+costs["exit_slip_rate"])/2
                g=geometry(spec,thesis,entry,side,tick,a5,a15,bars_15m,bars_1h,fee,slip,aligned,runner_boost=runner_boost,tp1_r=tp1_r)
                g["trail_start_r"] = adapt_trail_r
                # Tighter anti-chase cap on MOMENTUM prevents entering at candle extremes.
                chase_mult = 0.60 if thesis["family"] in ("MOMENTUM","EARLY_STRUCTURE_SHIFT","COUNTERTREND_WAVE","STRUCTURE_CONTINUATION") else 0.80
                if tf=="M5" and offset: chase_mult=min(chase_mult,.50)
                chase_atr=min(a5,atr(m5_at[:-1])) if tf=="M5" and offset else a5
                chase_cap=min(.50*g["risk"],max(2*tick,chase_mult*chase_atr))
                level=thesis.get("retest_level")
                if (not g["valid"] or abs(quote-ref)>chase_cap) and (not offset or tf!="M5" or abs(quote-ref)<=chase_cap) and level is not None and side*(quote-level)>max(2*tick,.30*aa) and side*(quote-level)<=.8*a5 and not adverse and side*(rows[-1].close-level)>0:
                    limit_entry=round_price(level,tick)
                    lc=rates(spec["symbol"],"LIMIT")
                    lf=(lc["entry_fee_rate"]+lc["exit_fee_rate"])/2; ls=(lc["entry_slip_rate"]+lc["exit_slip_rate"])/2
                    lg=geometry(spec,thesis,limit_entry,side,tick,a5,a15,bars_15m,bars_1h,lf,ls,aligned,runner_boost=runner_boost,tp1_r=tp1_r)
                    if lg["valid"]:
                        entry=limit_entry; route="LIMIT"; g=lg; costs=lc; fee=lf; slip=ls; g["trail_start_r"]=adapt_trail_r; maker_pullback=True; limit_ttl_min=15
                # Keep existing direct market execution (avoids adverse selection on strong impulses).
                stop,tp,actual_risk,rr,net_rr=(g[k] for k in ("stop","tp","risk","rr","net_rr"))
                if tf=="M5" and offset: chase_cap=min(chase_cap,.50*actual_risk)
                spread=ask-bid
                # No unsupported probability: rank by evidence + bounded causal estimates.
                meta=active_estimate(history or [],{**thesis,"regime":regime,"side":side,"symbol":spec["symbol"],"order_type":route},now_ms)
                rank=objective(meta,score,net_rr,route,thesis["rvol"],ms["phase"],side)
                exit_policy,shadow_exit_policy=exit_policies(thesis["family"],ms["phase"],rr,funding_regime)
                # Funding-aware TP1 geometry is recomputed consistently with selected route.
                g["tp1_r"]=exit_policy["tp1_r"]
                from mexc_geometry import compute_tp1_price
                g["tp1"]=compute_tp1_price(entry,actual_risk,side,tick,g["tp1_r"]) if actual_risk>0 else tp
                valid=(entry>0 and g["valid"] and actual_risk>tick and side*(structural-stop)>=-1e-9
                       and side*(quote-structural)>0 and spread<=.20*actual_risk and net_rr>=1.15
                       and (route=="LIMIT" and side*(quote-entry)>=tick*.5 or route=="MARKET" and abs(quote-ref)<=chase_cap) and not adverse)
                event_key=signal_event_key(spec["symbol"],side,known)
                signal_id=f"{spec['symbol']}:{side}:{thesis['family']}:{tf}:{thesis['trigger_ts']}"
                if thesis.get("research_variant"): signal_id+=":"+thesis["research_variant"]
                after_reason=delayed_guard(thesis,side,known,bars_1m,bars_5m,now_ms,chase_atr,tick)
                reasons=[]
                if not valid: reasons.append("GEOMETRY_OR_EXECUTION")
                if g["geometry_reason"]: reasons.extend(g["geometry_reason"].split(","))
                if spread>.20*actual_risk: reasons.append("WIDE_SPREAD")
                if abs(quote-ref)>chase_cap and (route=="MARKET" or tf=="M5" and offset): reasons.append("CHASE_DISTANCE")
                if adverse: reasons.append("OPPOSITE_IMPULSE")
                if after_reason: reasons.append(after_reason)
                if score<76: diag.setdefault("soft_evidence",[]).append("LOW_EVIDENCE_SCORE")
                # Opposed higher timeframes are context, not the trade direction. Require actual local evidence.
                if opposing and not (reversal or transition or local_support): reasons.append("NO_LOCAL_SHIFT_EVIDENCE")
                if wave_side==-side and not (reversal and offset==0 and score>=82): reasons.append("CURRENT_WAVE_OPPOSES")
                if tf=="M1" and not (aligned or local_support or (local_at["m5"]["side"]==side and reversal and score>=82)):
                    reasons.append("M1_NO_M5_CONTEXT")
                if tf=="M1" and thesis["family"]=="EXHAUSTION" and score<80: reasons.append("M1_WEAK_REVERSAL")
                used=consumed_signals or {}
                if signal_id in used or event_key in used: reasons.append("SIGNAL_CONSUMED")
                if known<=max((h.get("closed_ts",0) for h in (history or []) if h.get("symbol")==spec["symbol"]),default=0):
                    reasons.append("SIGNAL_BEFORE_LAST_EXIT")
                if thesis.get("research_variant"): reasons.append("RESEARCH_ONLY")
                actionable=not reasons
                if actionable: diag["ready"]+=1
                else: diag["rejections"].extend(reasons)
                reason=f"{thesis['setup_ua']} · {regime} · RVOL {thesis['rvol']:.2f}x"
                if not valid:
                    reason+=" · непридатна ціна/SL/spread/RR"
                    if "NEAR_LIQUIDITY" in g["geometry_reason"]: reason+=" · близький рівень ліквідності: недостатній RR"
                    if "DISTANT_INVALIDATION" in g["geometry_reason"]: reason+=" · структурний стоп надто далеко від входу"
                    if "INVALID_STRUCTURE" in g["geometry_reason"]: reason+=" · некоректна геометрія"
                if btc_opposes: reason+=" · BTC проти (вага, не блок)"
                if rel_support: reason+=" · відносна сила/слабкість підтверджує"
                if transition: reason+=" · підтверджений локальний перехід"
                if tf=="M5" and offset: reason+=" · попередня M5, повторна перевірка ціни"
                if reasons: reason+=" · "+", ".join(dict.fromkeys(reasons))
                if adverse: reason+=" · зустрічний імпульс"
                candidates.append({**thesis,**g,"status":"READY" if actionable else "WATCH",
                    "status_ua":("ЛІМІТКА ГОТОВА" if route=="LIMIT" else f"ВХІД ЗАРАЗ ({tf})") if actionable else "ЛИШЕ SHADOW" if thesis.get("research_variant") else "СПОСТЕРЕЖЕННЯ",
                    "score":score,"rank_score":round(rank,3),"side":side,"side_ua":"LONG ↑" if side==1 else "SHORT ↓",
                    "order_type":route,"entry":entry,"stop":stop,"tp":tp,"rr":round(rr,2),"net_rr_estimate":round(net_rr,3),
                    "regime":regime,"meta":meta,"btc_aligned":not btc_opposes,"reason_ua":reason,
                    "signal_id":signal_id,"signal_event_key":event_key,"rejection_codes":list(dict.fromkeys(reasons)),
                    "signal_known_ts":known,"trigger_offset":offset,"signal_age_sec":round((now_ms-known)/1000,1),
                    "local_support":local_support,"relative_support":rel_support,"chase_cap":round(chase_cap,8),
                    "alignment_tier":align_tier,"risk_multiplier":total_risk_mult,
                    "funding_rate":funding_rate,"funding_pct":funding_pct,"open_interest":round(open_interest,2),
                    "funding_regime":funding_regime,"funding_risk_mult":funding_risk_mult,"early_close_hint_ua":early_close_hint_ua,
                    "maker_pullback":maker_pullback,"limit_ttl_min":limit_ttl_min,**exit_policy,**costs,
                    "shadow_exit_policy":shadow_exit_policy,
                    "fee_rate":fee,"slippage_rate":slip,"tick":tick,"spread_cost_rate":max(0.,spread/(2*entry)) if entry>0 else 0.,
                    "sizing":compute_sizing(entry,stop,tp,deposit=deposit,risk_pct=risk_pct*total_risk_mult,max_leverage=max_leverage)})
    # Compare a genuinely executable passive retest with a market entry; use same structural geometry and costs.
    for c in list(candidates):
        if c.get("status")!="READY" or c.get("order_type")!="MARKET" or c.get("retest_level") is None: continue
        side=c["side"]; quote=ask if side==1 else bid; level=round_price(c["retest_level"],tick)
        distance=side*(quote-level)
        if not max(2*tick,.15*a5)<=distance<=.8*a5 or side*(level-c["structural_stop"])<=0: continue
        lc=rates(spec["symbol"],"LIMIT"); lf=(lc["entry_fee_rate"]+lc["exit_fee_rate"])/2; ls=(lc["entry_slip_rate"]+lc["exit_slip_rate"])/2
        lg=geometry(spec,c,level,side,tick,a5,a15,bars_15m,bars_1h,lf,ls,c.get("alignment_tier")=="ALIGNED",tp1_r=c["tp1_r"])
        if not lg["valid"] or lg["net_rr"]<1.15 or ask-bid>.20*lg["risk"]: continue
        meta=active_estimate(history or [],{**c,"symbol":spec["symbol"],"order_type":"LIMIT"},now_ms)
        # Pending fill probability is not calibrated. Conservative opportunity-cost penalty, not fake fill-rate.
        rank=objective(meta,c["score"],lg["net_rr"],"LIMIT",c["rvol"],ms["phase"],side)-.04-distance/max(a5,tick)*.04
        c2={**c,**lg,**lc,"entry":level,"order_type":"LIMIT","maker_pullback":True,"limit_ttl_min":15,"fee_rate":lf,"slippage_rate":ls,"rank_score":round(rank,4),"net_rr_estimate":lg["net_rr"],"meta":meta,"status_ua":"ЛІМІТКА ГОТОВА","reason_ua":c["reason_ua"]+" · альтернативний ретест LIMIT","sizing":compute_sizing(level,lg["stop"],lg["tp"],deposit=deposit,risk_pct=risk_pct*c["risk_multiplier"],max_leverage=max_leverage)}
        c2["trail_start_r"]=c["trail_start_r"]
        active_policy,trial=exit_policies(c2["family"],ms["phase"],lg["rr"],c2["funding_regime"])
        c2.update(active_policy,shadow_exit_policy=trial,rr=round(lg["rr"],2))
        c2["tp1"]=compute_tp1_price(level,lg["risk"],side,tick,trial["tp1_r"])
        candidates.append(c2)
    for d in diagnostics.values():
        d["rejections"]=list(dict.fromkeys(d["rejections"]))
    base_out["direction_diagnostics"]=diagnostics
    # Log previously elapsed M1 windows separately. They are not executable or assigned hypothetical PnL from current quote.
    base_out["elapsed_m1_windows"]=[{"ts":b.ts+60000,"side":1 if b.close>b.open else -1,"body_atr":round(abs(b.close-b.open)/max(atr(bars_1m[:i]),tick),3),"kind":"ELAPSED_M1_DIAGNOSTIC"} for i,b in enumerate(bars_1m) if i>=22 and 180000<=now_ms-b.ts-60000<900000 and abs(b.close-b.open)>=.7*max(atr(bars_1m[:i]),tick)]
    if not candidates:
        if base_out["observed_wave_side"]:
            base_out.update(status="WATCH",status_ua="ХВИЛЯ · ЧЕКАЄМО ТРИГЕР",
                reason_ua=("Локальна висхідна хвиля" if base_out["observed_wave_side"]==1 else "Локальна низхідна хвиля")+" · немає підтвердженого входу"+(" · M5 пауза" if not wave_side else ""))
        else: base_out.update(reason_ua="LONG та SHORT перевірено; немає підтвердженого тригера")
        return base_out
    candidates.sort(key=lambda c:(c["status"]=="READY",c["rank_score"],c["net_rr_estimate"]),reverse=True)
    best={**base_out,**candidates[0],"candidate_count":len(candidates),
          "candidate_diagnostics":[{k:c.get(k) for k in ("setup","family","trigger_tf","trigger_offset","signal_age_sec","side","status","score","rank_score","meta","reason_ua","rejection_codes","signal_id","signal_event_key","signal_known_ts","entry","stop","tp","risk","tick","order_type","valid","regime","limit_ttl_min","entry_fee_rate","exit_fee_rate","entry_slip_rate","exit_slip_rate","spread_cost_rate","retest_level","reference","research_variant","shadow_exit_policy","geometry_policy","trail_atr_m5","trail_start_r","fee_rate","slippage_rate","tp1")} for c in candidates]}
    best["tp1"]=None if not best.get("tp1_enabled") else best.get("tp1")
    best["be_trigger"]=None  # structural trailing has no fixed break-even trigger
    return best


def _slot_group(cluster: str, split_metal=True) -> str:
    return "OIL" if cluster=="ENERGY" else "METAL" if cluster=="METALS" and split_metal else "CRYPTO" if split_metal else "CRYPTO_METAL"


def migrate_metal_slot(memory):
    """Lossless migration; keep original entry/stop/R/model_id and old daily archive."""
    cm=memory.get("cm_active")
    if cm and (cm.get("cluster")=="METALS" or cm.get("symbol")=="XPT_USDT"):
        if memory.get("metal_active"):
            raise RuntimeError("Conflicting metal positions; manual ledger reconciliation required")
        memory["metal_active"]=cm; memory["cm_active"]=None
    old=memory.get("cm_history") or []
    moved=[h for h in old if h.get("symbol")=="XPT_USDT"]
    if moved:
        memory["metal_history"]=sorted((memory.get("metal_history") or [])+moved,key=lambda h:h.get("closed_ts",0))
        memory["cm_history"]=[h for h in old if h.get("symbol")!="XPT_USDT"]
    # Upgrade the old per-family identities so a second family cannot reopen the same bar.
    consumed=memory.get("consumed_signals",{})
    for key,value in list(consumed.items()):
        try:
            sym,side,_,tf,ts=key.split(":")
            if tf not in ("M1","M5"): continue
            event=signal_event_key(sym,int(side),int(ts)+(60000 if tf=="M1" else 300000))
            consumed[event]=max(value,consumed.get(event,0))
        except (ValueError,TypeError): continue


def _portfolio_limits(state):
    defaults=state.get("defaults")
    if not defaults: return None  # backwards-compatible unit/legacy fixtures; production always provides defaults
    deposit=float(defaults["deposit_usdt"]); lev=float(defaults["max_leverage"])
    risk_pct=float(os.getenv("BZU_PORTFOLIO_RISK_PCT",str(defaults["risk_pct"])))
    if not math.isfinite(risk_pct) or not 0<risk_pct<=100: raise ValueError("Invalid portfolio risk budget")
    return {"risk_usd":deposit*risk_pct/100,"notional_usd":deposit*lev,"deposit_usdt":deposit,"risk_pct":risk_pct}


def _reserved(memory):
    risk=notional=0.
    for key in ("oil_active","cm_active","metal_active"):
        a=memory.get(key)
        if not a: continue
        size=a.get("sizing") or {}; entry=float(a.get("entry") or 0); dist=float(a.get("risk") or 0)
        nominal=float(size.get("position_usdt") or 0)
        if not nominal and entry>0 and dist>0: nominal=float(size.get("risk_usd") or 0)*entry/dist
        if not all(math.isfinite(v) and v>0 for v in (entry,dist,nominal)):
            return math.inf,math.inf
        # Reserve initial stop risk and estimated round-trip costs even for pending orders / trailed positions.
        notional+=nominal
        risk+=nominal*(dist/entry+(float(a.get("fee_rate",0))+float(a.get("slippage_rate",0)))*(2+dist/entry)+float(a.get("spread_cost_rate",0))*(1+dist/entry))
    return risk,notional


def _cap_sizing(pick,memory,budget):
    if budget is None: return pick.get("sizing")
    used_risk,used_notional=_reserved(memory)
    if not (math.isfinite(used_risk) and math.isfinite(used_notional)): return None
    size=pick.get("sizing") or {}; old=float(size.get("position_usdt") or 0)
    entry=float(pick["entry"]); dist=abs(entry-float(pick["stop"]))
    fee=float(pick.get("fee_rate",0)); slip=float(pick.get("slippage_rate",0))
    sl_pct=dist/entry; cost_pct=(fee+slip)*(2+sl_pct)+float(pick.get("spread_cost_rate",0))*(1+sl_pct)
    # Reserve room for each of three independent clusters. Never increase requested sizing.
    meta=pick.get("meta") or {}
    fraction=1/3 + (min(1/6,max(0.,float(meta.get("expected_r_estimate") or 0)*.25)) if meta.get("effective_n",0)>=8 else 0.)
    cap_risk=min(budget["risk_usd"]*fraction,budget["risk_usd"]-used_risk)
    cap_nominal=min(budget["notional_usd"]*fraction,budget["notional_usd"]-used_notional)
    nominal=math.floor(max(0,min(old,cap_nominal,cap_risk/(sl_pct+cost_pct)))*100)/100
    if nominal<1 or sl_pct<=0: return None
    return {**size,"position_usdt":nominal,"effective_leverage":round(nominal/budget["deposit_usdt"],2),
            "risk_usd":round(nominal*sl_pct,4),"target_profit_usd":round(nominal*abs(float(pick["tp"])-entry)/entry,4),
            "portfolio_reserved_risk_usd":round(nominal*(sl_pct+cost_pct),6),"portfolio_capped":nominal+0.005<old}



def rank_and_lock_clusters(
    results: list[dict[str, Any]],
    *,
    oil_slot_busy: bool = False,
    cm_slot_busy: bool = False,
    metal_slot_busy: bool = False,
    split_metal: bool = True,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Rank independently; max one ready per cluster."""
    for r in results:
        if r.get("status")=="READY" and (r.get("research_variant") or r.get("family") in SHADOW_FAMILIES):
            r.update(status="WATCH",status_ua="ЛИШЕ SHADOW")
            r.setdefault("rejection_codes",[]).append("RESEARCH_ONLY")
    ranked = sorted(
        results,
        key=lambda r: (
            r["status"] == "IN_TRADE",
            r["status"] == "READY",
            r["status"] == "WATCH",
            r.get("rank_score", r["score"]),
            r["rr"] or 0.0,
        ),
        reverse=True,
    )
    active_groups: set[str] = set()
    if oil_slot_busy:
        active_groups.add("OIL")
    if cm_slot_busy:
        active_groups.add("CRYPTO" if split_metal else "CRYPTO_METAL")
    if metal_slot_busy:
        active_groups.add("METAL")
    occupied_groups=set(active_groups)

    for item in ranked:
        if item["status"] == "READY":
            grp = _slot_group(item["cluster"],split_metal)
            if grp in active_groups:
                item["status"] = "WATCH"
                item["status_ua"] = "СЛОТ ЗАЙНЯТО" if (grp in occupied_groups) else "ЗАЙНЯТО В КЛАСТЕРІ"
                item["cluster_locked"] = True
                item["reason_ua"] = item.get("reason_ua", "")+" · "+(
                    "Слот уже в угоді — чекаємо закриття"
                    if (grp in occupied_groups)
                    else "Сильніший сигнал уже активний у цій групі"
                )
                item.setdefault("rejection_codes",[]).append("CLUSTER_BUSY")
            else:
                active_groups.add(grp)
    top_pick = ranked[0] if ranked and ranked[0]["status"] in ("IN_TRADE", "READY", "WATCH") else None
    return ranked, top_pick


def _kyiv_day(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).astimezone(KYIV_TZ).strftime("%Y-%m-%d")


def _advance_single_slot(
    slot_key: str,
    group_name: str,
    state: dict[str, Any],
    memory: dict[str, Any],
    *,
    now_ms: int,
    split_metal: bool = True,
    budget: dict | None = None,
) -> dict[str, Any]:
    """Advance one paper slot, then admit a new candidate within shared budget."""
    today = _kyiv_day(now_ms)
    daily_key = f"{slot_key}_daily"
    active_key = f"{slot_key}_active"
    hist_key = f"{slot_key}_history"

    daily = memory.get(daily_key) or {}
    if daily.get("day") != today or daily.get("model_schema") != MODEL_SCHEMA:
        if daily and daily.get("trades"):
            memory.setdefault(f"{slot_key}_prior_daily",[]).append(daily)
        daily = {"day": today, "model_schema": MODEL_SCHEMA, "trades": 0, "wins": 0, "losses": 0, "net_r": 0.0, "net_usd": 0.0}

    active = memory.get(active_key)
    by_sym = {r["symbol"]: r for r in state.get("instruments", [])}

    # 1. Advance only unseen M1 bars using an immutable chronological cursor.
    closed_this_cycle=False
    if isinstance(active, dict) and active.get("symbol") in by_sym:
        item=by_sym[active["symbol"]]
        result=advance(active,item,now_ms,round_price=round_price,legacy_cursor=memory.get("last_run_ts",0))
        if active.get("opened_ts") is not None and not active.get("opened_kyiv"):
            active["opened_kyiv"]=datetime.fromtimestamp(active["opened_ts"]/1000,tz=timezone.utc).astimezone(KYIV_TZ).strftime("%d.%m %H:%M")
        if result:
            closed_this_cycle=True
            if result["reason"].startswith("LIMIT_"):
                memory.setdefault(f"{slot_key}_unfilled",[]).append({"signal_id":active.get("signal_id"),"reason":result["reason"],"quality":result["quality"],"ts":result["closed_ts"]})
            else:
                entry=float(active["entry"]); risk=float(active["risk"]); side=int(active["side"]); exit_price=result["exit"]
                if active.get("tp1_hit") and active.get("tp1_fill_price") is not None:
                    pp = float(active.get("partial_pct", 0.50))
                    g1 = side * (float(active["tp1_fill_price"]) - entry) / risk
                    g2 = side * (exit_price - entry) / risk
                    gross = pp * g1 + (1.0 - pp) * g2
                else:
                    gross = side * (exit_price - entry) / risk
                fee,slip=active.get("fee_rate",0.),active.get("slippage_rate",0.)
                cost=cost_r(active,exit_price)
                # Funding is not observed. Keep this explicitly an estimated net paper result.
                fund_r,fund_status=funding_accrual(active,item,result["closed_ts"]) if active.get("model_schema") in (MODEL_SCHEMA,PREVIOUS_SCHEMA) else (0.,"ESTIMATED_EX_FUNDING")
                net_r=round(gross-cost-fund_r,4); risk_usd=float((active.get("sizing") or {}).get("risk_usd") or 3.)
                net_usd=round(net_r*risk_usd,2)
                close_day=_kyiv_day(result["closed_ts"])
                history=memory.setdefault(hist_key,[])
                history.append({"symbol":active["symbol"],"label":active["label"],"cluster_ua":active["cluster_ua"],
                    "side":side,"side_ua":active["side_ua"],"setup":active["setup"],"setup_ua":active.get("setup_ua"),"family":active.get("family"),"regime":active.get("regime"),
                    "trade_id":active.get("trade_id"),"signal_event_key":active.get("signal_event_key"),"route":active.get("order_type"),"geometry_policy":active.get("geometry_policy"),"model_schema":active.get("model_schema"),
                    "trigger_tf":active.get("trigger_tf"),"trigger_offset":active.get("trigger_offset"),"signal_known_ts":active.get("signal_known_ts"),
                    "opened_ts":active.get("opened_ts"),"opened_kyiv":active.get("opened_kyiv"),"entry":entry,"exit":exit_price,
                    "tp1_hit":bool(active.get("tp1_hit")),"tp1_fill_price":active.get("tp1_fill_price"),
                    "alignment_tier":active.get("alignment_tier"),"risk_multiplier":active.get("risk_multiplier"),
                    "funding_regime":active.get("funding_regime"),"risk_usd":risk_usd,
                    "gross_r":round(gross,4),"cost_r_estimate":round(cost,4),"net_r":net_r,"net_usd":net_usd,"cost_status":fund_status,"funding_r":round(fund_r,4),
                    "reason":result["reason"],"reason_ua":close_reason_ua(result["reason"]),"quality":result["quality"] if not active.get("ambiguous_ohlc") else "AMBIGUOUS_OHLC",
                    "closed_ts":result["closed_ts"],"closed_kyiv":datetime.fromtimestamp(result["closed_ts"]/1000,tz=timezone.utc).astimezone(KYIV_TZ).strftime("%d.%m %H:%M")})
                if close_day==today and active.get("model_schema")==MODEL_SCHEMA:
                    daily["trades"]+=1; daily["wins"]+=int(net_r>0); daily["losses"]+=int(net_r<0)
                    daily["net_r"]=round(daily["net_r"]+net_r,4); daily["net_usd"]=round(daily["net_usd"]+net_usd,2)
            active=None
        else:
            cur=float(active.get("current_price") or active["entry"]); entry=float(active["entry"]); risk=float(active["risk"])
            cost=cost_r(active,cur)
            if active.get("phase")=="PENDING_LIMIT":
                pnl=0.
            elif active.get("tp1_hit") and active.get("tp1_fill_price") is not None:
                pp=float(active.get("partial_pct",0.50))
                g1=int(active["side"])*(float(active["tp1_fill_price"])-entry)/risk
                g2=int(active["side"])*(cur-entry)/risk
                pnl=round(pp*g1+(1.0-pp)*g2-cost,4)
            else:
                pnl=round(int(active["side"])*(cur-entry)/risk-cost,4)
            if active.get("model_schema") in (MODEL_SCHEMA,PREVIOUS_SCHEMA) and active.get("phase")!="PENDING_LIMIT":
                fund_r,fund_status=funding_accrual(active,item,now_ms); pnl=round(pnl-fund_r,4); active["funding_status"]=fund_status
            active.update(pnl_r=pnl,pnl_usd=round(pnl*float((active.get("sizing") or {}).get("risk_usd") or 3.),2))
    elif isinstance(active,dict):
        active["data_gap"]=True; active["quality"]="DATA_GAP"

    # 2. If slot is free, open best READY candidate in this group
    if active is None and not closed_this_cycle:
        candidates = [
            r for r in state.get("instruments", [])
            if _slot_group(r["cluster"],split_metal) == group_name and r["status"] == "READY"
            and not r.get("research_variant") and r.get("family") not in SHADOW_FAMILIES
            and r.get("entry") and r.get("stop") and r.get("tp")
            and (not r.get("signal_id") or r["signal_id"] not in memory.get("consumed_signals",{}))
            and (not r.get("signal_event_key") or r["signal_event_key"] not in memory.get("consumed_signals",{}))
            and (not r.get("quote_ts") or 0<=now_ms-r["quote_ts"]<=60000)
            and (not r.get("signal_known_ts") or 0<=now_ms-r["signal_known_ts"]<trigger_age_limit(r.get("trigger_tf")))
            and (not r.get("signal_known_ts") or r["signal_known_ts"]>max((h.get("closed_ts",0) for h in (memory.get(hist_key) or []) if h.get("symbol")==r["symbol"]),default=0))
        ]
        eligible=[]
        for candidate in sorted(candidates,key=lambda r:(r.get("rank_score",r["score"]),r.get("rr") or 0),reverse=True):
            size=_cap_sizing(candidate,memory,budget)
            if budget is not None and size is None:
                candidate.update(status="WATCH",status_ua="ЛІМІТ ПОРТФЕЛЯ",reason_ua=candidate.get("reason_ua","")+" · вичерпано спільний бюджет ризику/маржі")
                candidate.setdefault("rejection_codes",[]).append("PORTFOLIO_BUDGET")
                continue
            if size is not None: candidate["sizing"]=size
            eligible.append(candidate); break
        if eligible:
            pick=eligible[0]
            risk = abs(float(pick["entry"]) - float(pick["stop"]))
            if risk > 0:
                pending=pick["order_type"]=="LIMIT"
                fee,slip=(pick["fee_rate"],pick["slippage_rate"]) if "fee_rate" in pick and "slippage_rate" in pick else ((0.,0.) if (pick.get("maker_pullback") or pick.get("fee_tag")=="0% FEE") else (cost_rates() if pick.get("signal_id") else (0.,0.)))
                init_alert = None
                if pick.get("funding_regime") == "CROWDED_WITH_US" and pick.get("tp1_enabled",False):
                    init_alert = {
                        "code": "FUNDING_EARLY_TP1",
                        "level": "warn",
                        "ts": now_ms,
                        "title_ua": f"⚠ ПЕРЕГРІТИЙ FUNDING ({pick.get('funding_pct', 0):+.3f}%) · ДОСТРОКОВИЙ TP1 (+{pick.get('tp1_r', 0.85):.2f} R)",
                        "detail_ua": "Натовп перегрітий у наш бік: ризик знижено (×0.75), фіксуємо 50% раніше та переводимо в Б/У.",
                    }
                active = {
                    "phase": "PENDING_LIMIT" if pending else "OPEN",
                    "placed_ts":now_ms,"processed_until_ts":now_ms,"expires_ts":now_ms+int(pick.get("limit_ttl_min") or 8)*60000,
                    "stop_effective_ts":now_ms,"quality":"OBSERVED_M1",
                    "alignment_tier":pick.get("alignment_tier"),"risk_multiplier":pick.get("risk_multiplier"),
                    "funding_rate":pick.get("funding_rate",0.0),"funding_pct":pick.get("funding_pct",0.0),
                    "open_interest":pick.get("open_interest",0.0),"funding_regime":pick.get("funding_regime","NEUTRAL"),
                    "early_close_hint_ua":pick.get("early_close_hint_ua"),
                    "maker_pullback":bool(pick.get("maker_pullback",False)),
                    "tp1_enabled":False if pick.get("model_schema")==MODEL_SCHEMA else pick.get("tp1_enabled",False),"tp1_hit":False,"tp1":pick.get("tp1"),"tp1_r":pick.get("tp1_r",1.0),"partial_pct":pick.get("partial_pct",0.50),
                    "time_stop_enabled":False if pick.get("model_schema")==MODEL_SCHEMA else pick.get("time_stop_enabled",False),"idea_review_enabled":False if pick.get("model_schema")==MODEL_SCHEMA else pick.get("idea_review_enabled",False),"stale_tighten_min":pick.get("stale_tighten_min",120),"stale_exit_min":pick.get("stale_exit_min",180),"stale_tightened":False,
                    "action_alert":init_alert,
                    "model_schema":MODEL_SCHEMA,"trade_id":pick.get("signal_id",f"{pick['symbol']}:{now_ms}"),
                    "signal_id":pick.get("signal_id"),"signal_event_key":pick.get("signal_event_key"),"family":pick.get("family"),"regime":pick.get("regime"),
                    "trigger_tf":pick.get("trigger_tf"),"trigger_offset":pick.get("trigger_offset"),"signal_known_ts":pick.get("signal_known_ts"),
                    "fee_rate":fee,"slippage_rate":slip,**{k:pick[k] for k in ("entry_fee_rate","exit_fee_rate","entry_slip_rate","exit_slip_rate","spread_cost_rate") if k in pick},
                    "symbol": pick["symbol"],
                    "short": pick["short"],
                    "label": pick["label"],
                    "cluster": pick["cluster"],
                    "cluster_ua": pick["cluster_ua"],
                    "fee_tag": pick["fee_tag"],
                    "mexc_url": pick["mexc_url"],
                    "tick": pick["tick"],
                    "side": pick["side"],
                    "side_ua": pick["side_ua"],
                    "setup": pick["setup"],
                    "setup_ua": pick["setup_ua"],
                    "order_type": pick["order_type"],
                    "entry": float(pick["entry"]),
                    "stop": float(pick["stop"]),
                    "initial_stop": float(pick["stop"]),
                    "tp": float(pick["tp"]),
                    "be_trigger": None if pick.get("geometry_policy")==POLICY else pick.get("be_trigger"),
                    "geometry_policy":pick.get("geometry_policy"),"trail_start_ts":now_ms,
                    "trail_atr_m5":pick.get("trail_atr_m5"),"trail_start_r":pick.get("trail_start_r",1.25),
                    "target_kind":pick.get("target_kind"),"structural_anchor":pick.get("structural_anchor"),
                    "risk": risk,
                    "rr": pick["rr"],
                    "score": pick["score"],
                    "sizing": pick["sizing"],
                    "opened_ts": None if pending else now_ms,
                    "opened_kyiv": None if pending else datetime.fromtimestamp(now_ms / 1000, tz=timezone.utc).astimezone(KYIV_TZ).strftime("%d.%m %H:%M"),
                    "current_price": float(pick["price"]),
                    "pnl_r": 0.0,
                    "pnl_usd": 0.0,
                    "peak_r": 0.0,
                    "be_armed": False,
                }
                if pick.get("signal_id"):
                    memory.setdefault("consumed_signals",{})[pick["signal_id"]]=now_ms
                if pick.get("signal_event_key"):
                    memory.setdefault("consumed_signals",{})[pick["signal_event_key"]]=now_ms

    if closed_this_cycle:
        for row in state.get("instruments",[]):
            if row.get("status")=="READY" and _slot_group(row["cluster"],split_metal)==group_name:
                row.update(status="WATCH",status_ua="НОВИЙ ЦИКЛ ПІСЛЯ ВИХОДУ",reason_ua=row.get("reason_ua","")+" · очікуємо наступний цикл після закриття")
                row.setdefault("rejection_codes",[]).append("CYCLE_AFTER_CLOSE")

    # 3. Project active trade directly onto its instrument row so card & chart ALWAYS show its real levels!
    if isinstance(active, dict) and active.get("symbol") in by_sym:
        item = by_sym[active["symbol"]]
        item.update(
            status="IN_TRADE",
            status_ua="ЛІМІТКА ОЧІКУЄ" if active.get("phase")=="PENDING_LIMIT" else "В УГОДІ",
            lifecycle_quality=active.get("quality"),
            geometry_policy=active.get("geometry_policy"),target_kind=active.get("target_kind"),trail_reason=active.get("trail_reason"),
            side=active["side"],
            side_ua=active["side_ua"],
            setup=active["setup"],
            setup_ua=active["setup_ua"],
            order_type=active["order_type"],
            entry=active["entry"],
            stop=active["stop"],
            tp=active["tp"],
            be_trigger=active.get("be_trigger"),
            rr=active["rr"],
            score=active["score"],
            sizing=active["sizing"],
            initial_stop=active.get("initial_stop"),
            tp1=active.get("tp1") if active.get("tp1_enabled") or active.get("tp1_hit") else None, tp1_enabled=active.get("tp1_enabled",False), tp1_r=active.get("tp1_r", 1.0), tp1_hit=bool(active.get("tp1_hit")),
            tp1_fill_price=active.get("tp1_fill_price"), partial_pct=active.get("partial_pct", 0.50),
            be_armed=bool(active.get("be_armed")), stale_tightened=bool(active.get("stale_tightened")),
            time_stop_countdown_min=active.get("time_stop_countdown_min"),
            action_alert=active.get("action_alert"),
            alignment_tier=active.get("alignment_tier"), risk_multiplier=active.get("risk_multiplier"),
            funding_rate=active.get("funding_rate", 0.0), funding_pct=active.get("funding_pct", 0.0),
            open_interest=active.get("open_interest", 0.0), funding_regime=active.get("funding_regime", "NEUTRAL"),
            early_close_hint_ua=active.get("early_close_hint_ua"), maker_pullback=bool(active.get("maker_pullback")),
            opened_ts=active.get("opened_ts"), opened_kyiv=active.get("opened_kyiv"),
            signal_id=active.get("signal_id"),signal_event_key=active.get("signal_event_key"), phase=active.get("phase","OPEN"),
            model_schema=active.get("model_schema"),trigger_tf=active.get("trigger_tf"),trigger_offset=active.get("trigger_offset"),
            reason_ua=(
                ("Smart Maker лімітка очікує відкату (витрати оцінені)" if active.get("maker_pullback") else "Лімітка очікує виконання")
                if active.get("phase")=="PENDING_LIMIT"
                else f"Відкрито {active['opened_kyiv']} · PnL {active['pnl_r']:+.2f} R ({active['pnl_usd']:+.2f}$)"
                + (" · ⚡ TP1 частково зафіксовано + Б/У" if active.get("tp1_hit") else " · ⏱ Тайм-стоп: SL підтягнуто" if active.get("stale_tightened") else " · Структурний трейлінг" if active.get("trail_reason") else " · Б/У активовано" if active.get("be_armed") else "")
            ),
        )

    memory[active_key] = active
    memory[daily_key] = daily
    # Display all today's resolved trades, including a v15.0 position closed after upgrade.
    today_rows=[h for h in (memory.get(hist_key) or []) if isinstance(h.get("closed_ts"),int) and _kyiv_day(h["closed_ts"])==today]
    daily_all={"day":today,"trades":len(today_rows),"wins":sum(h.get("net_r",0)>0 for h in today_rows),
               "losses":sum(h.get("net_r",0)<0 for h in today_rows),"net_r":round(sum(h.get("net_r",0) for h in today_rows),4),
               "net_usd":round(sum(h.get("net_usd",0) for h in today_rows),2)}
    enriched_hist = [
        {**h, "slot": group_name, "reason_ua": h.get("reason_ua") or close_reason_ua(h.get("reason"))}
        for h in (memory.get(hist_key) or [])[-50:]
    ]
    return {
        "occupied": active is not None,
        "active": active,
        "daily": daily,"daily_all":daily_all,
        "last_closed": enriched_hist[-1] if enriched_hist else None,
        "history": enriched_hist,
    }


def update_dual_paper_slots(state: dict[str, Any], memory: dict[str, Any], *, now_ms: int, split_metal=True) -> dict[str, Any]:
    """Retained public function name; v15.3 runs independent oil, crypto and metal slots."""
    memory["consumed_signals"]={k:v for k,v in memory.get("consumed_signals",{}).items() if now_ms-v<86400000}
    if split_metal: migrate_metal_slot(memory)
    budget=_portfolio_limits(state) if split_metal else None
    groups=[("oil","OIL"),("cm","CRYPTO" if split_metal else "CRYPTO_METAL")]
    if split_metal: groups.append(("metal","METAL"))
    def group_order(pair):
        key,group=pair
        best=max((r.get("rank_score",r.get("score",0)) for r in state["instruments"] if r.get("status")=="READY" and _slot_group(r["cluster"],split_metal)==group),default=-1)
        return bool(memory.get(key+"_active")),best
    slots={}
    # Existing positions are advanced before new entries, then free slots by candidate rank.
    for key,group in sorted(groups,key=group_order,reverse=True):
        slots[key]=_advance_single_slot(key,group,state,memory,now_ms=now_ms,split_metal=split_metal,budget=budget)
    for row in state["instruments"]:
        if row.get("status")=="READY" and (row.get("signal_id") in memory["consumed_signals"] or row.get("signal_event_key") in memory["consumed_signals"]):
            row.update(status="WATCH",status_ua="СИГНАЛ ВЖЕ ВИКОРИСТАНО")
    ranked, top_pick = rank_and_lock_clusters(state["instruments"],oil_slot_busy=slots["oil"]["occupied"],
        cm_slot_busy=slots["cm"]["occupied"],metal_slot_busy=slots.get("metal",{}).get("occupied",False),split_metal=split_metal)
    state.update(instruments=ranked,top_pick=top_pick,ready_count=sum(r["status"]=="READY" for r in ranked),
        watch_count=sum(r["status"]=="WATCH" for r in ranked),oil_slot=slots["oil"],cm_slot=slots["cm"],
        slot_layout="THREE_CLUSTERS" if split_metal else "LEGACY_TWO")
    if split_metal: state["metal_slot"]=slots["metal"]
    if budget:
        used_r,used_n=_reserved(memory)
        state["portfolio"]={**budget,"used_risk_usd":round(used_r,4) if math.isfinite(used_r) else None,
                            "used_notional_usd":round(used_n,2) if math.isfinite(used_n) else None,
                            "status":"OK" if used_r<=budget["risk_usd"]+1e-6 and used_n<=budget["notional_usd"]+1e-6 else "LEGACY_EXCEEDS_BUDGET",
                            "per_slot_notional_cap":round(budget["notional_usd"]/2,2),"per_slot_risk_cap":round(budget["risk_usd"]/2,4),"allocation":"BASE_THIRD_ADAPTIVE_UP_TO_HALF"}
    state["closed_history"]=sorted([h for slot in slots.values() for h in slot.get("history",[])],key=lambda h:int(h.get("closed_ts") or 0),reverse=True)[:120]
    memory["last_run_ts"]=now_ms
    return state


def update_cm_paper_slot(state: dict[str, Any], memory: dict[str, Any], *, now_ms: int) -> dict[str, Any]:
    """Compatibility for original two-slot integrations only; production uses three slots."""
    return update_dual_paper_slots(state,memory,now_ms=now_ms,split_metal=False)


def format_telegram_signal(item: dict[str, Any], btc_macro: dict[str, Any], ranked: list[dict[str, Any]]) -> str:
    tick = item.get("tick") or 0.01
    side_emoji = "🟢" if item["side"] == 1 else "🔴"
    order_ua = "ЛІМІТ (PAPER)" if item["order_type"] == "LIMIT" else "РИНКОВИЙ (PAPER)"
    s = item.get("sizing") or {}
    entry_s = format_price(item["entry"], tick)
    stop_s = format_price(item["stop"], tick)
    tp_s = format_price(item["tp"], tick)

    lines = [
        f"{side_emoji} <b>BZU MULTI · {item['label']}</b>",
        f"Кластер: <b>{item['cluster_ua']}</b> · {item['fee_tag']} · Рейтинг: <b>{item['score']}/100</b>",
        "",
        f"Напрямок: <b>{item['side_ua']}</b> · Тип: <b>{order_ua}</b>",
        "Статус: ЛІМІТКА ОЧІКУЄ (не відкрита)" if item.get("phase")=="PENDING_LIMIT" else "Статус: PAPER / модельний вхід",
        f"Сетап: {item['setup_ua']}",
        f"Вхід: <code>{entry_s}</code>",
        f"Стоп: <code>{stop_s}</code> (−{s.get('sl_pct', 0):.2f}%)",
        f"Тейк: <code>{tp_s}</code> (+{s.get('tp_pct', 0):.2f}% · <b>R:R {item['rr']:.2f}</b>)",
        "",
        f"💼 <b>Розрахунок позиції (депозит ${s.get('deposit_usdt', 100):.0f}, ризик ${s.get('risk_usd', 3):.2f}):</b>",
        f"• Обсяг позиції: <b>${s.get('position_usdt', 0):.0f}</b> (плече ~{s.get('effective_leverage', 0):.2f}×)",
        f"• Очікуваний профіт на TP: <b>+${s.get('target_profit_usd', 0):.2f}</b>",
        "",
        f"🧭 Макро BTC: <b>{btc_macro['label_ua']}</b> (${btc_macro.get('price', 0):,.0f})",
    ]
    top3 = [f"{r['short']} ({r['status_ua']}, {r['score']})" for r in ranked[:3]]
    lines.append("📊 Топ-3 радара: " + " · ".join(top3))
    lines.append(f"🔗 <a href=\"{item['mexc_url']}\">Відкрити {item['symbol']} на MEXC</a>")
    return "\n".join(lines)


def send_telegram_message(text: str, token: str, chat_id: str) -> bool:
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            return resp.status == 200
    except urllib.error.URLError:
        return False


def run_scan(*, fetcher=_http_json, now_ms: int | None=None, deposit=100., risk_pct=3., max_leverage=5.,
             memory=None, apply_locks=True) -> dict[str, Any]:
    """Bounded parallel data collection, independent failures, quote fetched last."""
    started=time.monotonic(); cut=now_ms if now_ms is not None else int(time.time()*1000)
    memory=memory or {}; errors=[]; data={}; ticks={}; contract_specs={}; timings={}
    active_symbols={a["symbol"]:a for a in (memory.get("oil_active"),memory.get("cm_active"),memory.get("metal_active")) if a}
    trend_positions={}
    research_positions=[]
    for key in ("h1_trend_book","h1_close_book","mixed_trend_range_book","range_control_book","adaptive_h1_book","early_h1_m5_book","adaptive_range_book",*trend_research.LIVE_PROFILES):
        for sym,p in memory.get(key,{}).get("positions",{}).items():
            research_positions.append(p)
            old=trend_positions.get(sym)
            if old is None or p.get("processed_until_ts",cut)<old.get("processed_until_ts",cut): trend_positions[sym]=p
    # Extend the existing M1 catch-up window to the oldest cursor; no extra candle calls.
    for sym,p in trend_positions.items():
        prior=active_symbols.get(sym)
        if not prior or p.get("processed_until_ts",cut)<prior.get("processed_until_ts",cut): active_symbols[sym]=p
    tasks={}; counts={}; denied=set(); http_statuses={}; skipped=False
    def record_error(key, exc, retry=False):
        code=getattr(exc,"code",None)
        label="TICKER" if key[0]=="TICKER" else f"{key[0]}/{key[1]}"
        if isinstance(code,int):
            http_statuses[label]=code
            if code in (401,403,451): denied.add(key)
        errors.append(f"{label}: {'retry ' if retry else ''}{type(exc).__name__} {str(exc)[:120]}")
    # Confirm broad access denial using two different public endpoints BEFORE fan-out.
    # A denied endpoint is not retried in the same cycle. No alternate proxy/host bypass.
    try:
        ticks=fetch_contract_ticks(fetcher,contract_specs)
        if not ticks: errors.append("DETAIL/: empty response")
    except Exception as exc:
        record_error(("DETAIL",""),exc)
    if ("DETAIL","") in denied:
        try: fetch_all_tickers(fetcher=fetcher)
        except Exception as exc: record_error(("TICKER",""),exc)
        if ("TICKER","") in denied:
            skipped=True
            errors.append("MEXC: access denied on two public endpoints; remaining requests skipped")
    if not skipped:
        with ThreadPoolExecutor(max_workers=8) as pool:
            for sym,interval,count in [("BTC_USDT","Min15",100),("BTC_USDT","Min60",40)]+[
                (spec["symbol"],tf,count) for spec in INSTRUMENTS for tf,count in (("Min1",300),("Min5",130),("Min15",110),("Min60",145),("Hour4",35))]:
                if interval=="Min1" and sym in active_symbols:
                    # Catch up all unseen minutes across normal outages, up to the API window.
                    a=active_symbols[sym]; cursor=a.get("processed_until_ts",a.get("opened_ts") or a.get("placed_ts") or cut)
                    count=max(count,min(1400,(cut-int(cursor))//60000+5))
                counts[(sym,interval)]=count
                tasks[pool.submit(fetch_klines,sym,interval,count,cut,fetcher)]=(sym,interval)
            for future in as_completed(tasks):
                key=tasks[future]
                try:
                    value=future.result()
                    if not value: errors.append(f"{key[0]}/{key[1]}: empty response")
                    data[key]=value
                except Exception as exc: record_error(key,exc)
    # Retry transient failures only; don't retry forbidden/unauthorized endpoints.
    failed=[key for key in tasks.values() if not data.get(key) and key not in denied]
    if not ticks and ("DETAIL","") not in denied and not skipped: failed.append(("DETAIL",""))
    recovered=[]
    if failed:
        with ThreadPoolExecutor(max_workers=4) as pool:
            retry={}
            for sym,tf in failed:
                if sym=="DETAIL": retry[pool.submit(fetch_contract_ticks,fetcher,contract_specs)]=(sym,tf)
                else:
                    count=counts[(sym,tf)]
                    retry[pool.submit(fetch_klines,sym,tf,count,cut,fetcher)]=(sym,tf)
            for future in as_completed(retry):
                sym,tf=key=retry[future]
                try: value=future.result()
                except Exception as exc:
                    record_error(key,exc,retry=True); continue
                if value:
                    if sym=="DETAIL": ticks=value
                    else: data[key]=value
                    recovered.append(f"{sym}/{tf}")
                    errors=[e for e in errors if not e.startswith(f"{sym}/{tf}:")]
                    http_statuses.pop(f"{sym}/{tf}",None)
    # Derive only complete contiguous exchange candles; never fill gaps with invented prices.
    derived=[]
    for sym in {key[0] for key in counts}:
        for src,dst in (("Min1","Min5"),("Min5","Min15"),("Min15","Min60"),("Min60","Hour4")):
            key=(sym,dst)
            if key not in counts or data.get(key): continue
            rows=aggregate_bars(data.get((sym,src),[]),TF_SECONDS[src],TF_SECONDS[dst],cut)
            required={"Min5":25,"Min15":30,"Min60":25,"Hour4":9}[dst]
            if len(rows)>=required:
                data[key]=rows; derived.append(f"{sym}/{dst}")
                errors=[e for e in errors if not e.startswith(f"{sym}/{dst}:")]
    timings["candles_s"]=round(time.monotonic()-started,3)
    quote_start=time.monotonic()
    tickers={}
    if not skipped and ("TICKER","") not in denied:
        try: tickers=fetch_all_tickers(fetcher=fetcher)
        except Exception as exc: record_error(("TICKER",""),exc)
        if not tickers and ("TICKER","") not in denied:
            try:
                tickers=fetch_all_tickers(fetcher=fetcher)
                if tickers:
                    recovered.append("TICKER"); errors=[e for e in errors if not e.startswith("TICKER:")]
                    http_statuses.pop("TICKER",None)
            except Exception as exc: record_error(("TICKER",""),exc,retry=True)
    quote_ts=now_ms if now_ms is not None else int(time.time()*1000)
    timings["ticker_s"]=round(time.monotonic()-quote_start,3)
    for sym,ticker in tickers.items():
        try:
            stamp=int(ticker.get("timestamp") or quote_ts)
            if stamp<10_000_000_000: stamp*=1000
            ticker["_observed_ts"]=min(stamp,quote_ts) if stamp<=quote_ts+5000 else 0
        except (ValueError,TypeError): ticker["_observed_ts"]=0
    if not tickers and ("TICKER","") not in denied: errors.append("TICKER: empty response")
    btc15=data.get(("BTC_USDT","Min15"),[]); btc1=data.get(("BTC_USDT","Min60"),[])
    btc_price=float((tickers.get("BTC_USDT") or {}).get("lastPrice") or (btc15[-1].close if btc15 else 0.))
    btc_macro=analyze_btc_macro(btc15,btc1,btc_price)
    btc_fresh=all(rows and 0<=quote_ts-rows[-1].ts-step<=step*2 for rows,step in ((btc15,900000),(btc1,3600000)))
    if not btc_fresh: btc_macro.update(side=0,state="UNKNOWN",label_ua="Дані BTC недоступні")
    history=(memory.get("oil_history") or [])+(memory.get("cm_history") or [])+(memory.get("metal_history") or [])
    results=[]
    for spec in INSTRUMENTS:
        sym=spec["symbol"]
        try:
            res=analyze_instrument(spec,ticker=tickers.get(sym) or {},bars_1m=data.get((sym,"Min1"),[]),bars_5m=data.get((sym,"Min5"),[]),
                bars_15m=data.get((sym,"Min15"),[]),bars_1h=data.get((sym,"Min60"),[]),bars_4h=data.get((sym,"Hour4"),[]),
                tick=ticks.get(sym,.01 if "OIL" in sym or "XPT" in sym else .001),btc_macro=btc_macro,history=history,
                btc_bars_15m=btc15 if btc_fresh else [],consumed_signals=memory.get("consumed_signals",{}),prior_breaks=memory.get("opportunity_breaks",{}).get(sym,[]),
                now_ms=quote_ts,deposit=deposit,risk_pct=risk_pct,max_leverage=max_leverage)
            if sym not in ticks and res["status"]=="READY":
                res.update(status="WATCH",status_ua="НЕМає TICK",reason_ua="Не отримано крок ціни контракту")
                for d in res.get("candidate_diagnostics",[]): d.setdefault("rejection_codes",[]).append("MISSING_TICK")
            res["_trend_h1"]=mexc_h1_trend.pack(data.get((sym,"Min60"),[]))
            res["trend_tick_valid"]=sym in ticks
            res["contract_spec"]=contract_specs.get(sym)
            res["require_contract_spec"]=True
            fund_positions=[a for a in (memory.get("oil_active"),memory.get("cm_active"),memory.get("metal_active"),*research_positions) if a and a.get("symbol")==sym and a.get("opened_ts") is not None and a.get("model_schema") in (MODEL_SCHEMA,PREVIOUS_SCHEMA,mexc_h1_trend.SCHEMA)]
            if fund_positions and not denied:
                try:
                    url=f"{MEXC_BASE}/api/v1/contract/funding_rate/history?symbol={sym}&page_num=1&page_size=40"
                    funding=fetcher(url).get("data",{}); raw=funding.get("resultList")
                    if not isinstance(raw,list): raise ValueError("invalid funding history")
                    res["funding_events"]=[{"settleTime":int(e["settleTime"]),"fundingRate":float(e["fundingRate"])} for e in raw if e.get("symbol",sym)==sym and math.isfinite(float(e["fundingRate"])) and int(e["settleTime"])<=quote_ts]
                    res["funding_history_incomplete"]=bool(funding.get("totalCount",0)>len(raw) and (not raw or min(int(e["settleTime"]) for e in raw)>min(a["opened_ts"] for a in fund_positions)))
                except Exception as exc:
                    res["funding_data_status"]="UNAVAILABLE"; errors.append(f"{sym}/FUNDING: {type(exc).__name__}")
            results.append(res)
        except Exception as exc:
            errors.append(f"{sym}: {type(exc).__name__}")
    now=now_ms if now_ms is not None else int(time.time()*1000)
    if apply_locks: ranked,top=rank_and_lock_clusters(results)
    else: ranked,top=results,None
    timings["scan_s"]=round(time.monotonic()-started,3)
    return {"version":SCANNER_VERSION,"exchange":"MEXC","updated_ts":now,"scan_started_ts":cut,
        "updated_kyiv":datetime.fromtimestamp(now/1000,tz=timezone.utc).astimezone(KYIV_TZ).strftime("%d.%m.%Y %H:%M"),
        "health":{"status":"DEGRADED" if errors or any(r.get("status_ua")=="ДАНІ НЕСВІЖІ" or not r.get("candles_1m") for r in results) else "OK","timings":timings,"expected_interval_sec":int(os.getenv("MEXC_SCAN_INTERVAL_SEC","180")),"assets":len(results),"errors":len(errors),"recovered":recovered,"derived":derived,"source":"GITHUB_ACTIONS" if os.getenv("GITHUB_RUN_ID") else "WORKER_OR_LOCAL","run_id":os.getenv("GITHUB_RUN_ID"),"run_attempt":os.getenv("GITHUB_RUN_ATTEMPT"),"api_base":MEXC_BASE,"api_http_statuses":http_statuses,"access_denied":bool(denied),"fanout_skipped":skipped},
        "defaults":{"deposit_usdt":deposit,"risk_pct":risk_pct,"max_leverage":max_leverage},
        "btc_macro":btc_macro,"top_pick":top,"instruments":ranked,"ready_count":sum(r["status"]=="READY" for r in ranked),
        "watch_count":sum(r["status"]=="WATCH" for r in ranked),"errors":errors}


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def maybe_notify(state: dict[str, Any], memory_path: Path, *, cooldown_sec: int = 2700) -> list[str]:
    token = os.getenv("TELEGRAM_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return []
    mem: dict[str, Any] = {}
    if memory_path.is_file():
        try:
            mem = json.loads(memory_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError("Cannot safely read scanner memory for notification") from exc
    sent_map: dict[str, int] = mem.get("sent", {})
    now_sec = int(state.get("last_attempt_ts",state["updated_ts"])) // 1000
    notified: list[str] = []
    for item in state["instruments"]:
        if item["status"] != "IN_TRADE":
            continue
        sig_key = (item["signal_id"]+":"+str(item.get("phase","OPEN"))) if item.get("signal_id") else f"{item['symbol']}:{item['side']}:{item['setup']}"
        last_sent = int(sent_map.get(sig_key, 0))
        if (item.get("signal_id") and last_sent) or now_sec - last_sent < cooldown_sec:
            continue
        msg = format_telegram_signal(item, state["btc_macro"], state["instruments"])
        if send_telegram_message(msg, token, chat_id):
            sent_map[sig_key] = now_sec
            notified.append(item["symbol"])
    # Prune old keys (> 24h)
    mem["sent"] = {k: v for k, v in sent_map.items() if now_sec - int(v) < 86400}
    _atomic_write(memory_path, mem)
    return notified


def run_cycle(output: Path, memory_path: Path, notify=False, *, fetcher=_http_json):
    output.parent.mkdir(parents=True,exist_ok=True); memory_path.parent.mkdir(parents=True,exist_ok=True)
    with open(str(memory_path)+".lock","a") as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        mem=json.loads(memory_path.read_text(encoding="utf-8")) if memory_path.exists() else {}
        deposit=float(os.getenv("BZU_DEPOSIT_USDT","100")); risk=float(os.getenv("BZU_RISK_PCT","3")); lev=float(os.getenv("BZU_MAX_LEVERAGE","5"))
        if not all(math.isfinite(x) and x>0 for x in (deposit,risk,lev)): raise ValueError("Invalid sizing configuration")
        migrate_metal_slot(mem)
        # v15.5 uses rolling same-schema PAPER labels only; calibration artifacts are research-only.
        state=run_scan(fetcher=fetcher,deposit=deposit,risk_pct=risk,max_leverage=lev,memory=mem,apply_locks=False)
        # Preserve existing entry/initial SL/TP/R. Start new trailing only after migration time.
        for active in (mem.get("oil_active"),mem.get("cm_active"),mem.get("metal_active")):
            if active and active.get("model_schema")=="MEXC_HYBRID_15_0" and not active.get("geometry_policy"):
                active.update(geometry_policy=POLICY,trail_start_ts=state["updated_ts"],be_trigger=None)
        # Lifecycle still advances whenever M1 data exists, even if other HTF fetches fail.
        for row in state["instruments"]:
            if row.get("analysis_fresh"):
                mem.setdefault("opportunity_breaks",{})[row["symbol"]]=row.get("opportunity_breaks",[])
        shadow_step(mem,state["instruments"],state["updated_ts"])
        state["opportunity_intelligence"]=register(mem,state["instruments"],state["updated_ts"])
        update_dual_paper_slots(state,mem,now_ms=state["updated_ts"])
        for row in state["instruments"]:
            if row.get("candidate_count"):
                mem["opportunity_ledger"]["observations"].append({"ts":state["last_attempt_ts"] if "last_attempt_ts" in state else state["updated_ts"],"symbol":row["symbol"],"kind":"FINAL_DISPOSITION","status":row["status"],"rejections":row.get("rejection_codes",[]),"signal_id":row.get("signal_id")})
        # Matched-entry geometry research cannot write back to the active slots or learner.
        try:
            geometry_book=deepcopy(mem.get("geometry_research_book",{}))
            state["geometry_research"]=mexc_geometry_research.update(geometry_book,state["instruments"],(mem.get("oil_active"),mem.get("cm_active"),mem.get("metal_active")),state["updated_ts"])
            mem["geometry_research_book"]=geometry_book
        except Exception as exc:
            logging.exception("Matched geometry research failed")
            state["geometry_research"]={"mode":"ERROR","error":type(exc).__name__}
        # H1 is a separate hypothetical PAPER account: never writes M1/M5 slots or training labels.
        # Transactional copy: a failure cannot partially mutate the trend book or stop the legacy scanner.
        try:
            trend_book=deepcopy(mem.get("h1_trend_book",{}))
            trend_deposit=float(os.getenv("BZU_H1_DEPOSIT_USDT","100"))
            fee,slip=cost_rates()
            state["h1_trend"]=mexc_h1_trend.update(trend_book,state["instruments"],state["updated_ts"],deposit=trend_deposit,risk_pct=min(3.,float(os.getenv("BZU_H1_RISK_PCT","3"))),max_leverage=min(5.,lev),fee=fee,slip=slip)
            mem["h1_trend_book"]=trend_book
        except Exception as exc:
            logging.exception("H1 separate PAPER update failed")
            state["h1_trend"]={"mode":"ERROR","error":type(exc).__name__,"asof_ts":state["updated_ts"]}
        # Independent control accounts. Each uses its own hypothetical deposit; only MIXED
        # shares a portfolio across H1 and M15, never adds risk to the M1/M5 book.
        state["portfolio_research"]={}
        for key,profile in (("h1_close_book","CLOSE_PRIOR"),("mixed_trend_range_book","MIXED_PEAK_RANGE"),("range_control_book","RANGE_ONLY"),("adaptive_h1_book","ADAPTIVE_H1"),("early_h1_m5_book","EARLY_H1_M5"),("adaptive_range_book","ADAPTIVE_RANGE"),*trend_research.LIVE_PROFILES.items()):
            try:
                enabled=os.getenv("BZU_V160_RESEARCH","true").lower() in ("1","true","yes")
                if profile in trend_research.PROFILES and not enabled and key not in mem: continue
                book=deepcopy(mem.get(key,{})); fee,slip=cost_rates()
                result=mexc_h1_trend.update(book,state["instruments"],state["updated_ts"],deposit=float(os.getenv("BZU_H1_DEPOSIT_USDT","100")),risk_pct=min(3.,float(os.getenv("BZU_H1_RISK_PCT","3"))),max_leverage=min(5.,lev),fee=fee,slip=slip,profile=profile,allow_new=profile!="MIXED_PEAK_RANGE" and (profile not in trend_research.PROFILES or enabled))
                state["portfolio_research"][profile]=result; mem[key]=book
            except Exception as exc:
                logging.exception("Independent portfolio research failed: %s",profile)
                state["portfolio_research"][profile]={"mode":"ERROR","error":type(exc).__name__}
        # Execution bars are internal. Keep the public snapshot small and do not duplicate long catch-up history.
        for r in state["instruments"]:
            r.pop("candles_1m",None); r.pop("funding_events",None); r.pop("_trend_h1",None); r.pop("contract_spec",None); r.pop("require_contract_spec",None)
        # An unavailable market must not masquerade as a successful refresh.
        fresh_count=sum(bool(r.get("quote_valid")) and r.get("quote_ts",0)>=state["updated_ts"]-60000 for r in state["instruments"])
        state["health"]["fresh_quotes"]=fresh_count
        state["health"]["fresh_analysis_count"]=sum(bool(r.get("analysis_fresh")) for r in state["instruments"])
        state["last_attempt_ts"]=state["updated_ts"]
        if not fresh_count or not any(r.get("analysis_fresh") for r in state["instruments"]):
            state["health"]["status"]="FAILED_DATA"
            previous=json.loads(output.read_text(encoding="utf-8")) if output.exists() else {}
            state["last_attempt_ts"]=state["updated_ts"]
            state["updated_ts"]=previous.get("updated_ts",0); state["updated_kyiv"]=previous.get("updated_kyiv","")
        _atomic_write(memory_path,mem); _atomic_write(output,state)
        print(f"[MULTI-SCANNER] assets={len(state['instruments'])}/7 fresh={fresh_count} status={state['health']['status']} timings={state['health']['timings']} errors={state['errors']}",flush=True)
        if notify and state["health"]["status"]!="FAILED_DATA":
            try: maybe_notify(state,memory_path)
            except Exception as exc: logging.warning("Telegram delivery failed: %s",type(exc).__name__)
        return state


def main() -> int:
    parser=argparse.ArgumentParser(description="BZU Multi-Asset Hybrid Scanner (MEXC Futures PAPER)")
    parser.add_argument("--output",type=Path,default=Path("multi_scanner_state.json"))
    parser.add_argument("--memory",type=Path,default=Path("multi_scanner_memory.json"))
    parser.add_argument("--notify",action="store_true")
    args=parser.parse_args()
    run_cycle(args.output,args.memory,args.notify)
    return 0


if __name__=="__main__":
    raise SystemExit(main())
