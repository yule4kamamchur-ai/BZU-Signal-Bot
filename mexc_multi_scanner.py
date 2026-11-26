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
import hashlib
import json
import math
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
SCANNER_VERSION = "BZU MULTI v14.5"

INSTRUMENTS: tuple[dict[str, Any], ...] = (
    {
        "symbol": "UKOIL_USDT",
        "label": "UKOIL (Brent)",
        "short": "UKOIL",
        "cluster": "ENERGY",
        "cluster_ua": "НАФТА",
        "fee_tag": "0% Maker",
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
        "fee_tag": "0% FEE",
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
        "fee_tag": "0% Maker",
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
        "fee_tag": "0% Maker",
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
        "fee_tag": "0% Maker",
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
        "fee_tag": "0% Maker",
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


def _http_json(url: str, timeout: float = 12.0) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "BZU-MultiScanner/14.5", "Accept": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


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
            if all(math.isfinite(x) for x in (o, h, l, c, v)) and l > 0 and h >= l:
                bars.append(Bar(ts=ts, open=o, high=h, low=l, close=c, volume=max(0.0, v)))
        except (TypeError, ValueError):
            continue
    bars.sort(key=lambda b: b.ts)
    return bars


def fetch_klines(symbol: str, interval: str, bars_needed: int, now_ms: int, fetcher=_http_json) -> list[Bar]:
    step = TF_SECONDS[interval]
    end_sec = now_ms // 1000
    start_sec = end_sec - (bars_needed + 10) * step
    url = f"{MEXC_BASE}/api/v1/contract/kline/{urllib.parse.quote(symbol)}?interval={interval}&start={start_sec}&end={end_sec}"
    payload = fetcher(url)
    return parse_mexc_klines(payload, step, now_ms)


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
    decimals = max(0, min(8, int(round(-math.log10(tick))) if tick < 1 else 0))
    return round(round(price / tick) * tick, decimals)


def format_price(price: float | None, tick: float) -> str:
    if price is None or not math.isfinite(price):
        return "—"
    decimals = max(0, min(6, int(round(-math.log10(tick))) if 0 < tick < 1 else 2))
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

    if price <= 0 or len(bars_5m) < 25 or len(bars_15m) < 30 or len(bars_1h) < 25:
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

    # Evaluate both LONG (+1) and SHORT (-1) candidates, pick best structural thesis
    candidates: list[dict[str, Any]] = []
    for side in (1, -1):
        body5 = side * (last5.close - last5.open)
        width5 = max(tick, last5.high - last5.low)
        wick5 = (min(last5.open, last5.close) - last5.low) if side == 1 else (last5.high - max(last5.open, last5.close))

        swept = (
            (last5.low <= swing_low - tick and last5.close >= swing_low + tick)
            if side == 1
            else (last5.high >= swing_high + tick and last5.close <= swing_high - tick)
        )
        m5_reclaim = swept and a5 > 0 and body5 >= 0.45 * a5 and rvol5 >= spec["min_rvol"]
        m5_rejection = a5 > 0 and wick5 >= 0.38 * width5 and body5 >= tick and rvol5 >= spec["min_rvol"]

        fvg = _detect_fvg(bars_15m, side)
        near_fvg = bool(fvg and (fvg[0] - 0.35 * a15 <= price <= fvg[1] + 0.35 * a15))
        near_ema = bool(ema20_15 and abs(price - ema20_15) <= 0.60 * a15)
        near_vwap = bool(vwap and abs(price - vwap) <= 0.65 * a15)
        at_edge = (range_pos <= 0.25) if side == 1 else (range_pos >= 0.75)

        htf_aligned = (b4 == side and b1 == side)
        htf_partial = (b4 == side or b1 == side) and not (b4 == -side and b1 == -side)
        htf_opposing = (b4 == -side and b1 == -side)

        # BTC Macro Filter for altcoins
        btc_side = btc_macro.get("side", 0)
        btc_opposes = bool(spec["use_btc_filter"] and btc_side == -side)
        btc_supports = bool(spec["use_btc_filter"] and btc_side == side)

        setup = None
        setup_ua = None
        score = 35

        if m5_reclaim:
            setup = "M5_LIQUIDITY_SWEEP_RECLAIM"
            setup_ua = "M5 свіп ліквідності + реклайм"
            score += 28
        elif htf_aligned and (near_fvg or near_ema or near_vwap) and (m5_rejection or body5 >= 0.40 * a5):
            setup = "TREND_PULLBACK_FVG_VWAP"
            setup_ua = "Відкат за трендом до FVG/VWAP"
            score += 24
        elif at_edge and m5_rejection:
            setup = "RANGE_EDGE_SWEEP_REVERSAL"
            setup_ua = "Відбій від межі діапазону"
            score += 20
        elif htf_aligned and (near_fvg or near_ema or near_vwap):
            setup = "WATCH_PULLBACK_ZONE"
            setup_ua = "Підхід до трендової зони"
            score += 12
        else:
            continue

        if htf_aligned:
            score += 18
        elif htf_partial:
            score += 8
        elif htf_opposing:
            score -= 16

        if b15 == side:
            score += 6
        if rvol5 >= spec["min_rvol"] + 0.25:
            score += 7
        elif rvol5 >= spec["min_rvol"]:
            score += 4
        if (side == 1 and range_pos <= 0.45) or (side == -1 and range_pos >= 0.55):
            score += 5
        if btc_supports:
            score += 6
        if btc_opposes:
            score -= 22

        score = max(0, min(99, score))

        # Entry price & Maker-First Retest routing
        quote = ask if side == 1 else bid
        raw_entry = quote
        order_type = "MARKET"
        if setup == "WATCH_PULLBACK_ZONE":
            order_type = "LIMIT"
            if fvg:
                raw_entry = (fvg[0] + fvg[1]) / 2.0
            elif ema20_15:
                raw_entry = ema20_15
        else:
            # Execute immediately on confirmed M5 reclaim/rejection; avoid adverse-selection 50% retrace limits
            order_type = "MARKET"

        entry = round_price(raw_entry, tick)
        if entry <= 0:
            continue

        # Universal ATR + percentage-clamped Stop Geometry
        atr_stop = max(spec["atr_mult"] * a15, 1.5 * a5)
        min_stop = entry * spec["min_stop_pct"]
        max_stop = entry * spec["max_stop_pct"]
        if spec["cluster"] == "ENERGY":
            min_stop = max(0.20, min_stop)
            max_stop = min(0.75, max_stop)
        risk = max(min_stop, min(max_stop, atr_stop))

        # Place stop cleanly beyond the M5 sweep wick if tighter
        wick_risk = abs(entry - (last5.low - tick if side == 1 else last5.high + tick))
        if min_stop <= wick_risk <= max_stop:
            risk = max(risk, wick_risk)

        stop = round_price(entry - side * risk, tick)
        actual_risk = abs(entry - stop)
        if actual_risk <= tick:
            continue

        # Target RR scaled by HTF alignment & asset class
        target_rr = spec["max_rr"] if (htf_aligned and not btc_opposes) else spec["min_rr"]
        if spec["cluster"] == "ENERGY":
            hour = (now_ms // 3_600_000) % 24
            if 0 <= hour < 7:
                target_rr = 1.40
            elif 13 <= hour < 19 and htf_aligned:
                target_rr = 2.25
            else:
                target_rr = 1.85

        tp = round_price(entry + side * target_rr * actual_risk, tick)
        rr = round(abs(tp - entry) / actual_risk, 2)

        sizing = compute_sizing(
            entry,
            stop,
            tp,
            deposit=deposit,
            risk_pct=risk_pct,
            max_leverage=max_leverage,
        )

        # Final admission gate
        min_ready_score = 80 if setup == "M5_LIQUIDITY_SWEEP_RECLAIM" else 86
        is_actionable = (
            setup in ("M5_LIQUIDITY_SWEEP_RECLAIM", "TREND_PULLBACK_FVG_VWAP")
            and score >= min_ready_score
            and not btc_opposes
            and not htf_opposing
            and rr >= (1.35 if spec["cluster"] == "ENERGY" else spec["min_rr"] - 0.05)
        )
        if is_actionable:
            status, status_ua = "READY", ("ЛІМІТКА ГОТОВА" if order_type == "LIMIT" else "ВХІД ЗАРАЗ (A+)")
            reason_ua = f"{setup_ua} · RVOL {rvol5:.2f}x"
        elif btc_opposes:
            status, status_ua = "WATCH", "ФІЛЬТР BTC"
            reason_ua = f"BTC ({btc_macro['label_ua']}) проти напрямку {'LONG' if side == 1 else 'SHORT'}"
        elif score >= 58:
            status, status_ua = "WATCH", "ОЧІКУЄМО ПІДТВЕРДЖЕННЯ"
            reason_ua = f"{setup_ua} · очікуємо підтвердження M5"
        else:
            status, status_ua = "WAIT", "НЕМАЄ СИГНАЛУ"
            reason_ua = "Слабке співпадіння факторів"

        candidates.append({
            **base_out,
            "status": status,
            "status_ua": status_ua,
            "score": score,
            "side": side,
            "side_ua": "LONG ↑" if side == 1 else "SHORT ↓",
            "setup": setup,
            "setup_ua": setup_ua,
            "order_type": order_type,
            "entry": entry,
            "stop": stop,
            "tp": tp,
            "rr": rr,
            "btc_aligned": not btc_opposes,
            "reason_ua": reason_ua,
            "sizing": sizing,
        })

    def _pack_bars(rows):
        return [{"t": b.ts // 1000, "o": round_price(b.open, tick), "h": round_price(b.high, tick),
                 "l": round_price(b.low, tick), "c": round_price(b.close, tick)} for b in rows[-80:]]

    if not candidates:
        base_out["reason_ua"] = "Ціна в середині діапазону, немає свіпу чи ретесту"
        base_out["candles_15m"] = _pack_bars(bars_15m)
        base_out["candles_5m"] = _pack_bars(bars_5m)
        return base_out

    candidates.sort(key=lambda c: (c["status"] == "READY", c["score"], c["rr"] or 0.0), reverse=True)
    best = candidates[0]
    best["candles_15m"] = _pack_bars(bars_15m)
    best["candles_5m"] = _pack_bars(bars_5m)
    # Also compute BE+ trigger price (+0.90R) for chart visualization
    if best.get("entry") and best.get("stop") and best.get("side"):
        rk = abs(best["entry"] - best["stop"])
        best["be_trigger"] = round_price(best["entry"] + best["side"] * 0.90 * rk, tick)
    return best


def rank_and_lock_clusters(results: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Rank all instruments and enforce max 1 READY per cluster (Energy/Metals/Crypto)."""
    ranked = sorted(
        results,
        key=lambda r: (
            r["status"] == "READY",
            r["status"] == "WATCH",
            r["score"],
            r["rr"] or 0.0,
        ),
        reverse=True,
    )
    active_clusters: set[str] = set()
    for item in ranked:
        if item["status"] == "READY":
            if item["cluster"] in active_clusters:
                item["status"] = "WATCH"
                item["status_ua"] = "ЗАЙНЯТО В КЛАСТЕРІ"
                item["cluster_locked"] = True
                item["reason_ua"] = f"Сильніший сигнал уже активний у кластері {item['cluster_ua']}"
            else:
                active_clusters.add(item["cluster"])
    top_pick = ranked[0] if ranked and ranked[0]["status"] in ("READY", "WATCH") else None
    return ranked, top_pick


def format_telegram_signal(item: dict[str, Any], btc_macro: dict[str, Any], ranked: list[dict[str, Any]]) -> str:
    tick = item.get("tick") or 0.01
    side_emoji = "🟢" if item["side"] == 1 else "🔴"
    order_ua = "ЛІМІТ (0% Maker)" if item["order_type"] == "LIMIT" else "РИНКОВИЙ (M5)"
    s = item.get("sizing") or {}
    entry_s = format_price(item["entry"], tick)
    stop_s = format_price(item["stop"], tick)
    tp_s = format_price(item["tp"], tick)

    lines = [
        f"{side_emoji} <b>BZU MULTI · {item['label']}</b>",
        f"Кластер: <b>{item['cluster_ua']}</b> · {item['fee_tag']} · Рейтинг: <b>{item['score']}/100</b>",
        "",
        f"Напрямок: <b>{item['side_ua']}</b> · Тип: <b>{order_ua}</b>",
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
    top3 = [
        f"{r['short']} ({r['status_ua']}, {r['score']})"
        for r in ranked[:3]
    ]
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


def run_scan(
    *,
    fetcher=_http_json,
    now_ms: int | None = None,
    deposit: float = 100.0,
    risk_pct: float = 3.0,
    max_leverage: float = 5.0,
) -> dict[str, Any]:
    now = now_ms or int(time.time() * 1000)
    tickers = fetch_all_tickers(fetcher=fetcher)
    ticks = fetch_contract_ticks(fetcher=fetcher)

    # 1. Fetch BTC Macro context first
    btc_ticker = tickers.get("BTC_USDT") or {}
    btc_15m = fetch_klines("BTC_USDT", "Min15", 100, now, fetcher=fetcher)
    btc_1h = fetch_klines("BTC_USDT", "Min60", 40, now, fetcher=fetcher)
    btc_price = float(btc_ticker.get("lastPrice") or (btc_15m[-1].close if btc_15m else 0.0))
    btc_macro = analyze_btc_macro(btc_15m, btc_1h, btc_price)

    # 2. Scan all 7 basket instruments
    results: list[dict[str, Any]] = []
    errors: list[str] = []
    for spec in INSTRUMENTS:
        sym = spec["symbol"]
        try:
            b5 = fetch_klines(sym, "Min5", 60, now, fetcher=fetcher)
            b15 = fetch_klines(sym, "Min15", 110, now, fetcher=fetcher)
            b1h = fetch_klines(sym, "Min60", 45, now, fetcher=fetcher)
            b4h = fetch_klines(sym, "Hour4", 35, now, fetcher=fetcher)
            t_row = tickers.get(sym) or {}
            tick = ticks.get(sym, 0.01 if "OIL" in sym or "XPT" in sym else 0.001)
            res = analyze_instrument(
                spec,
                ticker=t_row,
                bars_5m=b5,
                bars_15m=b15,
                bars_1h=b1h,
                bars_4h=b4h,
                tick=tick,
                btc_macro=btc_macro,
                now_ms=now,
                deposit=deposit,
                risk_pct=risk_pct,
                max_leverage=max_leverage,
            )
            results.append(res)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{sym}: {exc}")

    ranked, top_pick = rank_and_lock_clusters(results)
    kyiv_str = datetime.fromtimestamp(now / 1000, tz=timezone.utc).astimezone(KYIV_TZ).strftime("%d.%m.%Y %H:%M")

    return {
        "version": SCANNER_VERSION,
        "exchange": "MEXC",
        "updated_ts": now,
        "updated_kyiv": kyiv_str,
        "defaults": {
            "deposit_usdt": deposit,
            "risk_pct": risk_pct,
            "max_leverage": max_leverage,
        },
        "btc_macro": btc_macro,
        "top_pick": top_pick,
        "instruments": ranked,
        "ready_count": sum(1 for r in ranked if r["status"] == "READY"),
        "watch_count": sum(1 for r in ranked if r["status"] == "WATCH"),
        "errors": errors,
    }


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
        except (OSError, ValueError):
            mem = {}
    sent_map: dict[str, int] = mem.get("sent", {})
    now_sec = state["updated_ts"] // 1000
    notified: list[str] = []
    for item in state["instruments"]:
        if item["status"] != "READY":
            continue
        sig_key = f"{item['symbol']}:{item['side']}:{item['setup']}"
        last_sent = int(sent_map.get(sig_key, 0))
        if now_sec - last_sent < cooldown_sec:
            continue
        msg = format_telegram_signal(item, state["btc_macro"], state["instruments"])
        if send_telegram_message(msg, token, chat_id):
            sent_map[sig_key] = now_sec
            notified.append(item["symbol"])
    # Prune old keys (> 24h)
    mem["sent"] = {k: v for k, v in sent_map.items() if now_sec - int(v) < 86400}
    mem["last_run_ts"] = state["updated_ts"]
    _atomic_write(memory_path, mem)
    return notified


def main() -> int:
    parser = argparse.ArgumentParser(description="BZU Multi-Asset Scanner (MEXC Futures)")
    parser.add_argument("--output", type=Path, default=Path("multi_scanner_state.json"))
    parser.add_argument("--memory", type=Path, default=Path("multi_scanner_memory.json"))
    parser.add_argument("--notify", action="store_true", help="Send Telegram alerts for READY A+ signals")
    args = parser.parse_args()

    deposit = float(os.getenv("BZU_DEPOSIT_USDT", "100"))
    risk_pct = float(os.getenv("BZU_RISK_PCT", "3.0"))
    max_lev = float(os.getenv("BZU_MAX_LEVERAGE", "5.0"))

    state = run_scan(deposit=deposit, risk_pct=risk_pct, max_leverage=max_lev)
    _atomic_write(args.output, state)

    notified = maybe_notify(state, args.memory) if args.notify else []
    top = state.get("top_pick")
    top_desc = f"{top['symbol']} {top['status_ua']} ({top['score']})" if top else "NONE"
    print(
        f"[MULTI-SCANNER] {len(state['instruments'])}/7 assets | READY={state['ready_count']} "
        f"| BTC={state['btc_macro']['state']} | TOP={top_desc} | notified={notified}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
