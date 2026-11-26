"""Unit tests for BZU Multi-Asset MEXC Scanner (Oil + Platinum + Crypto)."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import mexc_multi_scanner as ms


def _make_klines(start_sec: int, step_sec: int, count: int, base: float, drift: float, *, sweep_last: bool = False):
    times, opens, highs, lows, closes, vols = [], [], [], [], [], []
    p = base
    for i in range(count):
        t = start_sec + i * step_sec
        o = p
        c = p + drift
        h = max(o, c) + base * 0.0025
        l = min(o, c) - base * 0.0025
        v = 1000.0
        if sweep_last and i == count - 1:
            # Sweep below recent lows and close strongly higher with high RVOL
            l = min(lows[-8:]) - base * 0.003
            c = o + base * 0.006
            h = c + base * 0.001
            v = 2200.0
        times.append(t)
        opens.append(round(o, 4))
        highs.append(round(h, 4))
        lows.append(round(l, 4))
        closes.append(round(c, 4))
        vols.append(v)
        p = c
    return {"success": True, "data": {"time": times, "open": opens, "high": highs, "low": lows, "close": closes, "vol": vols}}


class MultiScannerTests(unittest.TestCase):
    def test_basket_has_all_seven_requested_instruments(self):
        syms = [s["symbol"] for s in ms.INSTRUMENTS]
        self.assertEqual(syms, [
            "UKOIL_USDT", "XPT_USDT", "SOL_USDT", "LINK_USDT", "LTC_USDT", "APT_USDT", "ORDI_USDT"
        ])

    def test_position_sizing_respects_risk_and_max_leverage(self):
        # Wide 1.5% stop on SOL: risk $3 -> $200 notional (2.0x leverage, under 5x cap)
        sz = ms.compute_sizing(200.0, 197.0, 206.0, deposit=100.0, risk_pct=3.0, max_leverage=5.0)
        self.assertEqual(sz["position_usdt"], 200.0)
        self.assertEqual(sz["effective_leverage"], 2.0)
        self.assertEqual(sz["risk_usd"], 3.0)
        self.assertEqual(sz["target_profit_usd"], 6.0)

        # Tight 0.3% stop on Oil: capped at max 5x leverage ($500 notional)
        sz_cap = ms.compute_sizing(100.0, 99.7, 100.6, deposit=100.0, risk_pct=3.0, max_leverage=5.0)
        self.assertEqual(sz_cap["position_usdt"], 500.0)
        self.assertEqual(sz_cap["effective_leverage"], 5.0)

    def test_full_scan_btc_macro_cluster_lock_and_telegram_format(self):
        now_sec = 1_795_700_000
        now_ms = now_sec * 1000

        bases = {
            "BTC_USDT": 92000.0,
            "UKOIL_USDT": 91.0,
            "XPT_USDT": 1320.0,
            "SOL_USDT": 185.0,
            "LINK_USDT": 18.5,
            "LTC_USDT": 88.0,
            "APT_USDT": 9.4,
            "ORDI_USDT": 28.0,
        }

        def fake_fetcher(url: str):
            if url.endswith("/api/v1/contract/ticker"):
                return {
                    "success": True,
                    "data": [
                        {
                            "symbol": sym,
                            "lastPrice": p * 1.02,
                            "bid1": p * 1.0199,
                            "ask1": p * 1.0201,
                            "riseFallRate": 0.021,
                            "volume24": 5_000_000,
                        }
                        for sym, p in bases.items()
                    ],
                }
            if url.endswith("/api/v1/contract/detail"):
                return {
                    "success": True,
                    "data": [{"symbol": sym, "priceUnit": 0.01 if p > 50 else 0.001} for sym, p in bases.items()],
                }
            # Kline endpoint
            for sym, base in bases.items():
                if f"/kline/{sym}?" in url:
                    for iv, step in ms.TF_SECONDS.items():
                        if f"interval={iv}" in url:
                            sweep = iv == "Min5" and sym in ("SOL_USDT", "LINK_USDT", "UKOIL_USDT")
                            return _make_klines(
                                now_sec - 120 * step,
                                step,
                                115,
                                base,
                                base * 0.0004,
                                sweep_last=sweep,
                            )
            raise AssertionError(f"Unexpected URL: {url}")

        state = ms.run_scan(fetcher=fake_fetcher, now_ms=now_ms, deposit=100.0, risk_pct=3.0, max_leverage=5.0)
        self.assertEqual(len(state["instruments"]), 7)
        self.assertEqual(state["btc_macro"]["state"], "BULLISH")

        # Cluster lock: at most 1 READY in CRYPTO cluster even though both SOL and LINK swept
        ready_crypto = [r for r in state["instruments"] if r["cluster"] == "CRYPTO" and r["status"] == "READY"]
        self.assertLessEqual(len(ready_crypto), 1)
        self.assertIsNotNone(state["top_pick"])

        # Telegram formatting includes exact numbers & MEXC link
        msg = ms.format_telegram_signal(state["top_pick"], state["btc_macro"], state["instruments"])
        self.assertIn("BZU MULTI-SCANNER", msg)
        self.assertIn("Розрахунок позиції", msg)
        self.assertIn("https://www.mexc.com/futures/", msg)

    def test_btc_bearish_blocks_altcoin_long(self):
        now_sec = 1_795_700_000
        now_ms = now_sec * 1000
        spec = next(s for s in ms.INSTRUMENTS if s["symbol"] == "SOL_USDT")
        b5 = ms.parse_mexc_klines(_make_klines(now_sec - 65 * 300, 300, 60, 180.0, 0.1, sweep_last=True), 300, now_ms)
        b15 = ms.parse_mexc_klines(_make_klines(now_sec - 115 * 900, 900, 110, 180.0, 0.1), 900, now_ms)
        b1h = ms.parse_mexc_klines(_make_klines(now_sec - 50 * 3600, 3600, 45, 180.0, 0.1), 3600, now_ms)
        b4h = ms.parse_mexc_klines(_make_klines(now_sec - 40 * 14400, 14400, 35, 180.0, 0.1), 14400, now_ms)
        btc_bear = {"symbol": "BTC_USDT", "price": 88000.0, "state": "BEARISH", "side": -1, "label_ua": "Ведмежий ↓"}
        res = ms.analyze_instrument(
            spec,
            ticker={"lastPrice": b5[-1].close, "bid1": b5[-1].close, "ask1": b5[-1].close, "riseFallRate": 0.01, "volume24": 1e6},
            bars_5m=b5,
            bars_15m=b15,
            bars_1h=b1h,
            bars_4h=b4h,
            tick=0.01,
            btc_macro=btc_bear,
            now_ms=now_ms,
        )
        self.assertNotEqual(res["status"], "READY")
        self.assertFalse(res["btc_aligned"])


if __name__ == "__main__":
    unittest.main()
