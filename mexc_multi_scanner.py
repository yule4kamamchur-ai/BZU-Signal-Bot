#!/usr/bin/env python3
"""BZU Multi-Asset Institutional Scanner for MEXC Futures (Oil + Platinum + Crypto).

Scans UKOIL_USDT, XPT_USDT (Platinum), SOL_USDT, LINK_USDT, LTC_USDT, APT_USDT,
and ORDI_USDT using MEXC public futures REST API (no API keys required), applies
a BTC_USDT macro regime filter, cluster correlation locks (max 1 Energy, 1 Metal,
1 Crypto), universal ATR/percentage risk geometry, fixed-dollar position sizing,
and sends deduplicated Ukrainian Telegram alerts for A+ setups.
"""
from __future__ import annotations

import argparse
import logging
import fcntl
from concurrent.futures import ThreadPoolExecutor, as_completed
from mexc_hybrid import MODEL_SCHEMA, detect, trend_efficiency, meta_estimate
from mexc_lifecycle import advance
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

MEXC_BASE = os.getenv("MEXC_FUTURES_BASE", "https://contract.mexc.com")
KYIV_TZ = ZoneInfo("Europe/Kyiv")
SCANNER_VERSION = "BZU MULTI v15.1 STRUCTURAL"

INSTRUMENTS: tuple[dict[str, Any], ...] = (
    {
        "symbol": "UKOIL_USDT",
        "label": "UKOIL (Brent)",
        "short": "UKOIL",
        "cluster": "ENERGY",
        "cluster_ua": "НАФТА",
        "fee_tag": "FEE: EST.",
        "min_stop_pct": 0.0022,
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
        "fee_tag": "FEE: EST.",
        "min_stop_pct": 0.0030,
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
        "fee_tag": "FEE: EST.",
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
        "fee_tag": "FEE: EST.",
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
        "fee_tag": "FEE: EST.",
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
        "fee_tag": "FEE: EST.",
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
        "fee_tag": "FEE: EST.",
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
        headers={"User-Agent": "BZU-MultiScanner/15.1", "Accept": "application/json"},
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


def fetch_contract_ticks(fetcher=_http_json) -> dict[str, float]:
    payload = fetcher(f"{MEXC_BASE}/api/v1/contract/detail")
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return {}
    out: dict[str, float] = {}
    for row in rows:
        sym = row.get("symbol")
        unit = row.get("priceUnit")
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
    fee=float(os.getenv("MEXC_TAKER_FEE_RATE","0.0005"))
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
    tick: float,
    btc_macro: dict[str, Any],
    now_ms: int,
    deposit: float = 100.0,
    risk_pct: float = 3.0,
    max_leverage: float = 5.0,
) -> dict[str, Any]:
    price = float(ticker.get("lastPrice") or (bars_5m[-1].close if bars_5m else 0.0))
    bid = float(ticker.get("bid1") or price)
    ask = float(ticker.get("ask1") or price)
    change24h = float(ticker.get("riseFallRate") or 0.0) * 100.0
    vol24h = float(ticker.get("volume24") or 0.0)

    base_out = {
        "symbol": spec["symbol"],
        "label": spec["label"],
        "short": spec["short"],
        "cluster": spec["cluster"],
        "cluster_ua": spec["cluster_ua"],
        "fee_tag": spec["fee_tag"],
        "mexc_url": spec["mexc_url"],
        "price": round_price(price, tick),
        "tick": tick,
        "change_24h_pct": round(change24h, 2),
        "volume_24h": round(vol24h, 2),
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
        return [{"t": b.ts//1000, "o": b.open, "h": b.high, "l": b.low, "c": b.close} for b in (rows[-limit:] if limit else rows)]
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
    vwap = session_vwap(bars_15m)
    ema20_15 = ema([b.close for b in bars_15m], 20)

    recent_24h = bars_15m[-96:]
    r_low = min(b.low for b in recent_24h)
    r_high = max(b.high for b in recent_24h)
    range_pos = (price - r_low) / (r_high - r_low) if r_high > r_low else 0.5

    last5 = bars_5m[-1]
    prev5_slice = bars_5m[-16:-1]
    avg_vol5 = sum(b.volume for b in prev5_slice) / max(1, len(prev5_slice))
    rvol5 = (last5.volume / avg_vol5) if avg_vol5 > 0 else 1.0

    prev_swings = bars_5m[-10:-1]
    swing_low = min(b.low for b in prev_swings)
    swing_high = max(b.high for b in prev_swings)

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
    regime="TREND_UP" if t1==1 and t4!=-1 else "TREND_DOWN" if t1==-1 and t4!=1 else "RANGE_TRANSITION"
    candidates=[]
    for side in (1,-1):
        aligned=b1==side and b4==side
        opposing=b1==-side and b4==-side
        trend=(b1==side or t1==side) and not opposing
        btc_side=btc_macro.get("side",0)
        btc_opposes=bool(spec["use_btc_filter"] and btc_side==-side)
        fvg=_detect_fvg(bars_15m,side)
        zones=[x for x in (ema20_15,vwap,(sum(fvg)/2 if fvg else None)) if x is not None]
        windows=[("M1",(bars_1m or [])[:-offset] if offset else (bars_1m or []),60000) for offset in (0,1,2)]
        windows.append(("M5",bars_5m,300000))
        for tf, rows, step in windows:
            if len(rows)<22 or not 0<=now_ms-(rows[-1].ts+step)<=(180000 if tf=="M1" else 300000):
                continue
            if any(b.ts-a.ts!=step for a,b in zip(rows[-22:],rows[-21:])):
                continue
            aa=atr(rows[:-1],14)
            zone=min(zones,key=lambda x:abs(x-rows[-1].close)) if zones else None
            if zone is not None and abs(zone-rows[-1].close)>.65*a15:
                zone=None
            for thesis in detect(rows,side=side,atr=aa,tick=tick,min_rvol=spec["min_rvol"],trend=trend,zone=zone,timeframe=tf):
                reversal=thesis["family"] in ("LIQUIDITY_SWEEP","FAILED_BREAKOUT","EXHAUSTION")
                score=thesis["base_score"] + (8 if aligned else 3 if trend else -4 if opposing else 0)
                score+=4 if b15==side else 0
                score+=3 if thesis["rvol"]>=spec["min_rvol"]+.25 else 0
                score+=(-7 if btc_opposes else 3 if spec["use_btc_filter"] and btc_side==side else 0)
                score+=2 if tf=="M5" else 0
                score-=min(3,int(max(0,now_ms-thesis["trigger_ts"]-step)//60000))
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
                fee,slip=cost_rates()
                g=geometry(spec,thesis,entry,side,tick,a5,a15,bars_15m,bars_1h,fee,slip,aligned)
                # MARKET first when location and structural payoff are already acceptable.
                # A wide stop must not make a late/chased entry look acceptable.
                chase_cap=min(.50*g["risk"],max(2*tick,.80*a5))
                level=thesis.get("retest_level")
                if (not g["valid"] or abs(quote-ref)>chase_cap) and level is not None and side*(quote-level)>max(2*tick,.30*aa) and side*(quote-level)<=.8*a5 and not adverse and side*(rows[-1].close-level)>0:
                    limit_entry=round_price(level,tick)
                    lg=geometry(spec,thesis,limit_entry,side,tick,a5,a15,bars_15m,bars_1h,fee,slip,aligned)
                    if lg["valid"]:
                        entry=limit_entry; route="LIMIT"; g=lg
                stop,tp,actual_risk,rr,net_rr=(g[k] for k in ("stop","tp","risk","rr","net_rr"))
                spread=ask-bid
                # No unsupported probability: rank by evidence + bounded causal estimates.
                meta=meta_estimate(history or [],{**thesis,"regime":regime},now_ms)
                rank=score+meta["rank_adjustment"]+min(2.,max(-3.,(net_rr-1.5)*2))
                valid=(entry>0 and g["valid"] and actual_risk>tick and side*(structural-stop)>=-1e-9
                       and side*(quote-structural)>0 and spread<=.20*actual_risk and net_rr>=1.15
                       and (route=="LIMIT" and side*(quote-entry)>=tick*.5 or route=="MARKET" and abs(quote-ref)<=chase_cap) and not adverse)
                actionable=valid and score>=76 and (not opposing or reversal) and (not btc_opposes or reversal and score>=79)
                if spec["use_btc_filter"] and btc_macro.get("state")=="UNKNOWN": actionable=False
                if tf=="M1" and thesis["family"]=="EXHAUSTION":
                    actionable=actionable and score>=80
                reason=f"{thesis['setup_ua']} · {regime} · RVOL {thesis['rvol']:.2f}x"
                if not valid:
                    reason+=" · непридатна ціна/SL/spread/RR"
                    if "NEAR_LIQUIDITY" in g["geometry_reason"]: reason+=" · близький рівень ліквідності: недостатній RR"
                    if "DISTANT_INVALIDATION" in g["geometry_reason"]: reason+=" · структурний стоп надто далеко від входу"
                    if "INVALID_STRUCTURE" in g["geometry_reason"]: reason+=" · некоректна геометрія"
                if btc_opposes: reason+=" · BTC проти"
                if adverse: reason+=" · зустрічний імпульс"
                candidates.append({**thesis,**g,"status":"READY" if actionable else "WATCH",
                    "status_ua":("ЛІМІТКА ГОТОВА" if route=="LIMIT" else f"ВХІД ЗАРАЗ ({tf})") if actionable else "СПОСТЕРЕЖЕННЯ",
                    "score":score,"rank_score":round(rank,3),"side":side,"side_ua":"LONG ↑" if side==1 else "SHORT ↓",
                    "order_type":route,"entry":entry,"stop":stop,"tp":tp,"rr":round(rr,2),"net_rr_estimate":round(net_rr,3),
                    "regime":regime,"meta":meta,"btc_aligned":not btc_opposes,"reason_ua":reason,
                    "signal_id":f"{spec['symbol']}:{side}:{thesis['family']}:{tf}:{thesis['trigger_ts']}",
                    "signal_known_ts":thesis["trigger_ts"]+step,"sizing":compute_sizing(entry,stop,tp,deposit=deposit,risk_pct=risk_pct,max_leverage=max_leverage)})
    if not candidates:
        base_out.update(reason_ua="Немає підтвердженого сетапу",regime=regime)
        return base_out
    candidates.sort(key=lambda c:(c["status"]=="READY",c["rank_score"],c["net_rr_estimate"]),reverse=True)
    best={**base_out,**candidates[0],"candidate_count":len(candidates),
          "candidate_diagnostics":[{k:c[k] for k in ("setup","family","trigger_tf","side","status","score","rank_score","meta","reason_ua")} for c in candidates]}
    best["be_trigger"]=None  # structural trailing has no fixed break-even trigger
    return best


def _slot_group(cluster: str) -> str:
    return "OIL" if cluster == "ENERGY" else "CRYPTO_METAL"


def rank_and_lock_clusters(
    results: list[dict[str, Any]],
    *,
    oil_slot_busy: bool = False,
    cm_slot_busy: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Rank all 7 instruments and enforce max 1 READY in OIL and max 1 in CRYPTO+METALS."""
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
        active_groups.add("CRYPTO_METAL")

    for item in ranked:
        if item["status"] == "READY":
            grp = _slot_group(item["cluster"])
            if grp in active_groups:
                item["status"] = "WATCH"
                item["status_ua"] = "СЛОТ ЗАЙНЯТО" if ((grp == "OIL" and oil_slot_busy) or (grp == "CRYPTO_METAL" and cm_slot_busy)) else "ЗАЙНЯТО В КЛАСТЕРІ"
                item["cluster_locked"] = True
                item["reason_ua"] = (
                    "Слот уже в угоді — чекаємо закриття"
                    if ((grp == "OIL" and oil_slot_busy) or (grp == "CRYPTO_METAL" and cm_slot_busy))
                    else "Сильніший сигнал уже активний у цій групі"
                )
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
) -> dict[str, Any]:
    """Advance 1 paper position slot (either OIL or CRYPTO_METAL) and record daily stats."""
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
                gross=side*(exit_price-entry)/risk
                fee,slip=active.get("fee_rate",0.),active.get("slippage_rate",0.)
                cost=(fee+slip)*(entry+exit_price)/risk
                # Funding is not observed. Keep this explicitly an estimated net paper result.
                net_r=round(gross-cost,4); risk_usd=float((active.get("sizing") or {}).get("risk_usd") or 3.)
                net_usd=round(net_r*risk_usd,2)
                close_day=_kyiv_day(result["closed_ts"])
                history=memory.setdefault(hist_key,[])
                history.append({"symbol":active["symbol"],"label":active["label"],"cluster_ua":active["cluster_ua"],
                    "side":side,"side_ua":active["side_ua"],"setup":active["setup"],"family":active.get("family"),"regime":active.get("regime"),
                    "trade_id":active.get("trade_id"),"geometry_policy":active.get("geometry_policy"),"model_schema":active.get("model_schema"),"opened_ts":active.get("opened_ts"),"entry":entry,"exit":exit_price,
                    "gross_r":round(gross,4),"cost_r_estimate":round(cost,4),"net_r":net_r,"net_usd":net_usd,"cost_status":"ESTIMATED_EX_FUNDING",
                    "reason":result["reason"],"quality":result["quality"] if not active.get("ambiguous_ohlc") else "AMBIGUOUS_OHLC",
                    "closed_ts":result["closed_ts"],"closed_kyiv":datetime.fromtimestamp(result["closed_ts"]/1000,tz=timezone.utc).astimezone(KYIV_TZ).strftime("%d.%m %H:%M")})
                if close_day==today and active.get("model_schema")==MODEL_SCHEMA:
                    daily["trades"]+=1; daily["wins"]+=int(net_r>0); daily["losses"]+=int(net_r<0)
                    daily["net_r"]=round(daily["net_r"]+net_r,4); daily["net_usd"]=round(daily["net_usd"]+net_usd,2)
            active=None
        else:
            cur=float(active.get("current_price") or active["entry"]); entry=float(active["entry"]); risk=float(active["risk"])
            cost=(active.get("fee_rate",0.)+active.get("slippage_rate",0.))*(entry+cur)/risk
            pnl=0. if active.get("phase")=="PENDING_LIMIT" else round(int(active["side"])*(cur-entry)/risk-cost,4)
            active.update(pnl_r=pnl,pnl_usd=round(pnl*float((active.get("sizing") or {}).get("risk_usd") or 3.),2))
    elif isinstance(active,dict):
        active["data_gap"]=True; active["quality"]="DATA_GAP"

    # 2. If slot is free, open best READY candidate in this group
    if active is None and not closed_this_cycle:
        candidates = [
            r for r in state.get("instruments", [])
            if _slot_group(r["cluster"]) == group_name and r["status"] == "READY"
            and r.get("entry") and r.get("stop") and r.get("tp")
            and (not r.get("signal_id") or r["signal_id"] not in memory.get("consumed_signals",{}))
            and (not r.get("quote_ts") or 0<=now_ms-r["quote_ts"]<=60000)
            and (not r.get("signal_known_ts") or now_ms-r["signal_known_ts"]<=(180000 if r.get("trigger_tf")=="M1" else 300000))
            and (not r.get("signal_known_ts") or r["signal_known_ts"]>max((h.get("closed_ts",0) for h in (memory.get(hist_key) or []) if h.get("symbol")==r["symbol"]),default=0))
        ]
        if candidates:
            pick = max(candidates,key=lambda r:(r.get("rank_score",r["score"]),r.get("rr") or 0))
            risk = abs(float(pick["entry"]) - float(pick["stop"]))
            if risk > 0:
                pending=pick["order_type"]=="LIMIT"
                fee,slip=cost_rates() if pick.get("signal_id") else (0.,0.)
                active = {
                    "phase": "PENDING_LIMIT" if pending else "OPEN",
                    "placed_ts":now_ms,"processed_until_ts":now_ms,"expires_ts":now_ms+8*60000,
                    "stop_effective_ts":now_ms,"quality":"OBSERVED_M1",
                    "model_schema":MODEL_SCHEMA,"trade_id":pick.get("signal_id",f"{pick['symbol']}:{now_ms}"),
                    "signal_id":pick.get("signal_id"),"family":pick.get("family"),"regime":pick.get("regime"),
                    "fee_rate":fee,"slippage_rate":slip,
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
            signal_id=active.get("signal_id"), phase=active.get("phase","OPEN"),
            reason_ua=("Лімітка очікує виконання" if active.get("phase")=="PENDING_LIMIT" else f"Відкрито {active['opened_kyiv']} · PnL {active['pnl_r']:+.2f} R ({active['pnl_usd']:+.2f}$)" + (" · Структурний трейлінг" if active.get("trail_reason") else " · Б/У активовано" if active.get("be_armed") else "")),
        )

    memory[active_key] = active
    memory[daily_key] = daily
    # Display all today's resolved trades, including a v15.0 position closed after upgrade.
    today_rows=[h for h in (memory.get(hist_key) or []) if isinstance(h.get("closed_ts"),int) and _kyiv_day(h["closed_ts"])==today]
    daily_all={"day":today,"trades":len(today_rows),"wins":sum(h.get("net_r",0)>0 for h in today_rows),
               "losses":sum(h.get("net_r",0)<0 for h in today_rows),"net_r":round(sum(h.get("net_r",0) for h in today_rows),4),
               "net_usd":round(sum(h.get("net_usd",0) for h in today_rows),2)}
    return {
        "occupied": active is not None,
        "active": active,
        "daily": daily,"daily_all":daily_all,
        "last_closed": (memory.get(hist_key) or [])[-1] if memory.get(hist_key) else None,
    }


def update_dual_paper_slots(state: dict[str, Any], memory: dict[str, Any], *, now_ms: int) -> dict[str, Any]:
    """Maintain BOTH unified MEXC slots: Slot 1 (OIL) and Slot 2 (CRYPTO+METALS)."""
    memory["consumed_signals"]={k:v for k,v in memory.get("consumed_signals",{}).items() if now_ms-v<86400000}
    oil_slot = _advance_single_slot("oil", "OIL", state, memory, now_ms=now_ms)
    cm_slot = _advance_single_slot("cm", "CRYPTO_METAL", state, memory, now_ms=now_ms)
    for row in state["instruments"]:
        if row.get("status")=="READY" and row.get("signal_id") in memory["consumed_signals"]:
            row.update(status="WATCH",status_ua="СИГНАЛ ВЖЕ ВИКОРИСТАНО")
    ranked, top_pick = rank_and_lock_clusters(
        state["instruments"],
        oil_slot_busy=oil_slot["occupied"],
        cm_slot_busy=cm_slot["occupied"],
    )
    state["instruments"] = ranked
    state["top_pick"] = top_pick
    state["ready_count"] = sum(1 for r in ranked if r["status"] == "READY")
    state["watch_count"] = sum(1 for r in ranked if r["status"] == "WATCH")
    state["oil_slot"] = oil_slot
    state["cm_slot"] = cm_slot
    memory["last_run_ts"]=now_ms
    return state


def update_cm_paper_slot(state: dict[str, Any], memory: dict[str, Any], *, now_ms: int) -> dict[str, Any]:
    """Backward-compatible wrapper for Slot 2 tests."""
    return update_dual_paper_slots(state, memory, now_ms=now_ms)



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
    memory=memory or {}; errors=[]; data={}; ticks={}; timings={}
    active_symbols={a["symbol"]:a for a in (memory.get("oil_active"),memory.get("cm_active")) if a}
    tasks={}; counts={}
    with ThreadPoolExecutor(max_workers=8) as pool:
        tasks[pool.submit(fetch_contract_ticks,fetcher)]=("DETAIL","")
        for sym,interval,count in [("BTC_USDT","Min15",100),("BTC_USDT","Min60",40)]+[
            (spec["symbol"],tf,count) for spec in INSTRUMENTS for tf,count in (("Min1",180),("Min5",130),("Min15",110),("Min60",145),("Hour4",35))]:
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
                if key[0]=="DETAIL": ticks=value
                else: data[key]=value
            except Exception as exc:
                errors.append(f"{key[0]}/{key[1]}: {type(exc).__name__} {str(exc)[:100]}")
    # Retry missing requests once, independently, with pacing in the real HTTP transport.
    failed=[key for key in tasks.values() if (not ticks if key[0]=="DETAIL" else not data.get(key))]
    recovered=[]
    if failed:
        with ThreadPoolExecutor(max_workers=4) as pool:
            retry={}
            for sym,tf in failed:
                if sym=="DETAIL": retry[pool.submit(fetch_contract_ticks,fetcher)]=(sym,tf)
                else:
                    count=counts[(sym,tf)]
                    retry[pool.submit(fetch_klines,sym,tf,count,cut,fetcher)]=(sym,tf)
            for future in as_completed(retry):
                sym,tf=key=retry[future]
                try: value=future.result()
                except Exception as exc:
                    errors.append(f"{sym}/{tf}: retry {type(exc).__name__} {str(exc)[:100]}"); continue
                if value:
                    if sym=="DETAIL": ticks=value
                    else: data[key]=value
                    recovered.append(f"{sym}/{tf}")
                    errors=[e for e in errors if not e.startswith(f"{sym}/{tf}:")]
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
    try: tickers=fetch_all_tickers(fetcher=fetcher)
    except Exception as exc:
        tickers={}; errors.append(f"TICKER: {type(exc).__name__}")
    if not tickers:
        try:
            tickers=fetch_all_tickers(fetcher=fetcher)
            if tickers:
                recovered.append("TICKER"); errors=[e for e in errors if not e.startswith("TICKER:")]
        except Exception as exc:
            errors.append(f"TICKER: retry {type(exc).__name__}")
    quote_ts=now_ms if now_ms is not None else int(time.time()*1000)
    timings["ticker_s"]=round(time.monotonic()-quote_start,3)
    for sym,ticker in tickers.items():
        try:
            stamp=int(ticker.get("timestamp") or quote_ts)
            if stamp<10_000_000_000: stamp*=1000
            ticker["_observed_ts"]=min(stamp,quote_ts) if stamp<=quote_ts+5000 else 0
        except (ValueError,TypeError): ticker["_observed_ts"]=0
    if not tickers: errors.append("TICKER: empty response")
    btc15=data.get(("BTC_USDT","Min15"),[]); btc1=data.get(("BTC_USDT","Min60"),[])
    btc_price=float((tickers.get("BTC_USDT") or {}).get("lastPrice") or (btc15[-1].close if btc15 else 0.))
    btc_macro=analyze_btc_macro(btc15,btc1,btc_price)
    btc_fresh=all(rows and 0<=quote_ts-rows[-1].ts-step<=step*2 for rows,step in ((btc15,900000),(btc1,3600000)))
    if not btc_fresh: btc_macro.update(side=0,state="UNKNOWN",label_ua="Дані BTC недоступні")
    history=(memory.get("oil_history") or [])+(memory.get("cm_history") or [])
    results=[]
    for spec in INSTRUMENTS:
        sym=spec["symbol"]
        try:
            res=analyze_instrument(spec,ticker=tickers.get(sym) or {},bars_1m=data.get((sym,"Min1"),[]),bars_5m=data.get((sym,"Min5"),[]),
                bars_15m=data.get((sym,"Min15"),[]),bars_1h=data.get((sym,"Min60"),[]),bars_4h=data.get((sym,"Hour4"),[]),
                tick=ticks.get(sym,.01 if "OIL" in sym or "XPT" in sym else .001),btc_macro=btc_macro,history=history,
                now_ms=quote_ts,deposit=deposit,risk_pct=risk_pct,max_leverage=max_leverage)
            if sym not in ticks and res["status"]=="READY":
                res.update(status="WATCH",status_ua="НЕМає TICK",reason_ua="Не отримано крок ціни контракту")
            results.append(res)
        except Exception as exc:
            errors.append(f"{sym}: {type(exc).__name__}")
    now=now_ms if now_ms is not None else int(time.time()*1000)
    if apply_locks: ranked,top=rank_and_lock_clusters(results)
    else: ranked,top=results,None
    timings["scan_s"]=round(time.monotonic()-started,3)
    return {"version":SCANNER_VERSION,"exchange":"MEXC","updated_ts":now,"scan_started_ts":cut,
        "updated_kyiv":datetime.fromtimestamp(now/1000,tz=timezone.utc).astimezone(KYIV_TZ).strftime("%d.%m.%Y %H:%M"),
        "health":{"status":"DEGRADED" if errors or any(r.get("status_ua")=="ДАНІ НЕСВІЖІ" or not r.get("candles_1m") for r in results) else "OK","timings":timings,"expected_interval_sec":int(os.getenv("MEXC_SCAN_INTERVAL_SEC","300")),"assets":len(results),"errors":len(errors),"recovered":recovered,"derived":derived,"source":"GITHUB_ACTIONS" if os.getenv("GITHUB_RUN_ID") else "WORKER_OR_LOCAL","run_id":os.getenv("GITHUB_RUN_ID"),"run_attempt":os.getenv("GITHUB_RUN_ATTEMPT")},
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
        state=run_scan(fetcher=fetcher,deposit=deposit,risk_pct=risk,max_leverage=lev,memory=mem,apply_locks=False)
        # Preserve existing entry/initial SL/TP/R. Start new trailing only after migration time.
        for active in (mem.get("oil_active"),mem.get("cm_active")):
            if active and active.get("model_schema")=="MEXC_HYBRID_15_0" and not active.get("geometry_policy"):
                active.update(geometry_policy=POLICY,trail_start_ts=state["updated_ts"],be_trigger=None)
        # Lifecycle still advances whenever M1 data exists, even if other HTF fetches fail.
        update_dual_paper_slots(state,mem,now_ms=state["updated_ts"])
        # Execution bars are internal. Keep the public snapshot small and do not duplicate long catch-up history.
        for r in state["instruments"]:
            r.pop("candles_1m",None)
        # An unavailable market must not masquerade as a successful refresh.
        fresh_count=sum(bool(r.get("quote_valid")) and r.get("quote_ts",0)>=state["updated_ts"]-60000 for r in state["instruments"])
        state["health"]["fresh_quotes"]=fresh_count
        state["last_attempt_ts"]=state["updated_ts"]
        if not fresh_count or not any(r.get("analysis_fresh") for r in state["instruments"]):
            state["health"]["status"]="FAILED_DATA"
            previous=json.loads(output.read_text(encoding="utf-8")) if output.exists() else {}
            state["last_attempt_ts"]=state["updated_ts"]
            state["updated_ts"]=previous.get("updated_ts",0); state["updated_kyiv"]=previous.get("updated_kyiv","")
        _atomic_write(memory_path,mem); _atomic_write(output,state)
        print(f"[MULTI-SCANNER] assets={len(state['instruments'])}/7 fresh={fresh_count} status={state['health']['status']} timings={state['health']['timings']} errors={state['errors']}",flush=True)
        if notify:
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
