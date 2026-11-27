"""Unit & integration tests for BZU MULTI v15.2 Institutional Upgrades."""
from __future__ import annotations
import unittest
from pathlib import Path

from mexc_geometry import POLICY, compute_tp1_price
from mexc_lifecycle import advance
from mexc_multi_scanner import (
    Bar,
    INSTRUMENTS,
    analyze_instrument,
    close_reason_ua,
    round_price,
    update_dual_paper_slots,
)


def _bar_dict(t_ms: int, o: float, h: float, l: float, c: float) -> dict:
    return {"t": t_ms // 1000, "o": o, "h": h, "l": l, "c": c}


class InstitutionalV152Tests(unittest.TestCase):
    def test_tp1_50_percent_and_auto_be_then_be_stop_positive(self):
        t0 = 1_700_000_040_000  # aligned to minute
        active = {
            "phase": "OPEN",
            "side": 1,
            "entry": 100.0,
            "stop": 99.0,
            "initial_stop": 99.0,
            "tp": 102.5,
            "tp1": 101.0,
            "tp1_r": 1.0,
            "tp1_enabled": True,
            "tp1_hit": False,
            "partial_pct": 0.50,
            "risk": 1.0,
            "tick": 0.01,
            "opened_ts": t0,
            "processed_until_ts": t0,
            "geometry_policy": POLICY,
            "fee_rate": 0.0,
            "slippage_rate": 0.0,
            "be_armed": False,
            "time_stop_enabled": True,
        }
        # Bar 1 hits TP1 (101.0) -> triggers 50% lock + moves SL to BE (>= 100.01)
        # Bar 2 reverses to 100.0 -> exits remaining 50% at BE with reason TP1_BE_STOP!
        item = {
            "tick": 0.01,
            "price": 100.0,
            "quote_ts": t0 + 120_000,
            "quote_valid": True,
            "candles_1m": [
                _bar_dict(t0, 100.0, 101.1, 99.9, 100.9),
                _bar_dict(t0 + 60_000, 100.9, 100.95, 99.95, 100.0),
            ],
        }
        res = advance(active, item, t0 + 120_000, round_price=round_price)
        self.assertIsNotNone(res)
        self.assertTrue(active["tp1_hit"])
        self.assertEqual(res["reason"], "TP1_BE_STOP")
        self.assertGreaterEqual(active["stop"], 100.01)
        self.assertIn("ФІКСУЄМО 50%", active["action_alert"]["title_ua"])

    def test_tp1_then_runner_tp(self):
        t0 = 1_700_000_040_000
        active = {
            "phase": "OPEN",
            "side": 1,
            "entry": 100.0,
            "stop": 99.0,
            "tp": 102.5,
            "tp1": 101.0,
            "tp1_r": 1.0,
            "tp1_enabled": True,
            "tp1_hit": False,
            "partial_pct": 0.50,
            "risk": 1.0,
            "tick": 0.01,
            "opened_ts": t0,
            "processed_until_ts": t0,
            "geometry_policy": POLICY,
            "fee_rate": 0.0,
            "slippage_rate": 0.0,
            "be_armed": False,
        }
        item = {
            "tick": 0.01,
            "price": 102.6,
            "quote_ts": t0 + 120_000,
            "quote_valid": True,
            "candles_1m": [
                _bar_dict(t0, 100.0, 101.2, 99.9, 101.1),
                _bar_dict(t0 + 60_000, 101.1, 102.6, 101.0, 102.5),
            ],
        }
        res = advance(active, item, t0 + 120_000, round_price=round_price)
        self.assertIsNotNone(res)
        self.assertEqual(res["reason"], "TP1_RUNNER_TP")
        self.assertEqual(res["exit"], 102.5)

    def test_alpha_decay_time_stop_tightens_and_exits(self):
        t0 = 1_700_000_100_000
        active = {
            "phase": "OPEN",
            "side": 1,
            "entry": 100.0,
            "stop": 99.0,
            "tp": 102.5,
            "tp1": 101.0,
            "tp1_r": 1.0,
            "tp1_enabled": True,
            "tp1_hit": False,
            "risk": 1.0,
            "tick": 0.01,
            "opened_ts": t0,
            "processed_until_ts": t0,
            "geometry_policy": POLICY,
            "time_stop_enabled": True,
            "stale_tighten_min": 45,
            "stale_exit_min": 60,
            "fee_rate": 0.0,
            "slippage_rate": 0.0,
        }
        # 46 minutes of flat chop around 100.05 -> should tighten stop and set warning banner
        candles_46 = [_bar_dict(t0 + i * 60_000, 100.0, 100.15, 99.92, 100.05) for i in range(46)]
        res1 = advance(active, {"tick": 0.01, "price": 100.05, "quote_ts": t0 + 46 * 60_000, "quote_valid": True, "candles_1m": candles_46}, t0 + 46 * 60_000, round_price=round_price)
        self.assertIsNone(res1)
        self.assertTrue(active.get("stale_tightened"))
        self.assertGreater(active["stop"], 99.0)
        self.assertIn("ТАЙМ-СТОП", active["action_alert"]["title_ua"])

        # Advance to 60 minutes still flat -> STALE_EXIT
        candles_60 = [_bar_dict(t0 + i * 60_000, 100.02, 100.12, 99.95, 100.04) for i in range(46, 61)]
        res2 = advance(active, {"tick": 0.01, "price": 100.04, "quote_ts": t0 + 61 * 60_000, "quote_valid": True, "candles_1m": candles_60}, t0 + 61 * 60_000, round_price=round_price)
        self.assertIsNotNone(res2)
        self.assertEqual(res2["reason"], "STALE_EXIT")

    def test_closed_history_exported_with_reason_ua(self):
        t0 = 1_700_000_040_000
        mem = {
            "oil_active": None,
            "cm_active": {
                "phase": "OPEN",
                "symbol": "LINK_USDT",
                "short": "LINK",
                "label": "Chainlink (LINK)",
                "cluster": "CRYPTO",
                "cluster_ua": "КРИПТА",
                "side": 1,
                "side_ua": "LONG ↑",
                "setup": "BREAKOUT_RETEST",
                "setup_ua": "Ретест пробою",
                "order_type": "MARKET",
                "entry": 10.0,
                "stop": 9.9,
                "tp": 10.25,
                "tp1": 10.1,
                "tp1_r": 1.0,
                "tp1_enabled": True,
                "tp1_hit": False,
                "partial_pct": 0.50,
                "risk": 0.1,
                "rr": 2.5,
                "score": 85,
                "tick": 0.001,
                "sizing": {"risk_usd": 3.0, "position_usdt": 300.0, "effective_leverage": 3.0},
                "opened_ts": t0,
                "opened_kyiv": "27.11 10:00",
                "processed_until_ts": t0,
                "geometry_policy": POLICY,
                "model_schema": "MEXC_HYBRID_V15_0",
                "fee_rate": 0.0,
                "slippage_rate": 0.0,
            },
            "oil_history": [],
            "cm_history": [],
        }
        state = {
            "instruments": [
                {
                    "symbol": "LINK_USDT",
                    "cluster": "CRYPTO",
                    "status": "NO_SETUP",
                    "score": 0,
                    "tick": 0.001,
                    "price": 10.0,
                    "quote_ts": t0 + 120_000,
                    "quote_valid": True,
                    "candles_1m": [
                        _bar_dict(t0, 10.0, 10.12, 9.99, 10.10),
                        _bar_dict(t0 + 60_000, 10.10, 10.11, 9.98, 10.00),
                    ],
                }
            ]
        }
        out = update_dual_paper_slots(state, mem, now_ms=t0 + 120_000)
        self.assertEqual(len(out["closed_history"]), 1)
        row = out["closed_history"][0]
        self.assertEqual(row["reason"], "TP1_BE_STOP")
        self.assertEqual(row["reason_ua"], close_reason_ua("TP1_BE_STOP"))
        self.assertGreater(row["net_usd"], 0.0)  # 50% locked at +1.0R + 50% at BE -> positive USD!

    def test_frontend_usd_coloring_and_history_modal_present(self):
        js = Path("multi_scanner.js").read_text(encoding="utf-8")
        html = Path("index.html").read_text(encoding="utf-8")
        self.assertIn("dTot.className = totU > 0 ? 'ms-up' : totU < 0 ? 'ms-dn' : ''", js)
        self.assertNotIn("dTot.className = totR > 0", js)
        self.assertIn("openHistoryModal", js)
        self.assertIn('id="ms-history-modal"', html)
        self.assertIn('id="ms-slot-oil-alert"', html)


if __name__ == "__main__":
    unittest.main()
