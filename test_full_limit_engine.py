"""Behavioral regression tests. No network, credentials or live orders."""
import copy
import ast
import hashlib
import json
import math
import tempfile
import unittest
from dataclasses import replace, asdict
from functools import wraps
from pathlib import Path
from unittest.mock import patch

import full_limit_engine as b
import bot_oneshot as core

NOW = 1_791_288_000_000  # fixed time; fixtures have exactly aligned candle closes
NOW = NOW//b.TF["4H"]*b.TF["4H"]


def preservation_ast_dump(node):
    """Render the existing Python 3.12 manifest format across Python versions.

    Python 3.11 lacks FunctionDef/ClassDef.type_params; Python 3.13 also
    changes ast.dump's empty-field defaults. Keep every original semantic
    field and normalize only the missing, empty type-parameter field.
    Existing manifest hashes remain authoritative and are never regenerated
    from the bot being checked. Source locations are intentionally omitted.
    """
    def render(value):
        if isinstance(value, ast.AST):
            names = list(value._fields)
            has_type_params = isinstance(value, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            if has_type_params and 'type_params' not in names:
                names.append('type_params')
            parts = []
            for name in names:
                if name == 'type_params' and has_type_params and not hasattr(value, name):
                    item = []
                elif hasattr(value, name):
                    item = getattr(value, name)
                else:
                    continue
                if item is None and getattr(type(value), name, ...) is None:
                    continue
                parts.append(name+'='+render(item))
            return type(value).__name__+'('+', '.join(parts)+')'
        if isinstance(value, list):
            return '['+', '.join(render(item) for item in value)+']'
        return repr(value)
    if not isinstance(node, ast.AST):
        raise TypeError('Expected AST node')
    return render(node)


def candle(ts, o=101, h=102, l=100.5, c=101, confirmed=True):
    return b.Candle(ts, o, h, l, c, 100, confirmed)


def snapshot(now=NOW, price=103):
    rows = {tf: [candle(now-(100-i)*step, 101, 102, 100, 101) for i in range(100)]
            for tf, step in b.TF.items()}
    return b.Snapshot(now, price, rows, price-.01, price+.01, True, now, "BZ-USDT-SWAP")


def order(side=1, placed=NOW, expiry=None):
    entry, stop, tp = (100, 99, 102) if side == 1 else (100, 101, 98)
    return {"id": "test-order", "event_key": "test-event", "zone_key": "test-zone",
            "instrument": "BZ-USDT-SWAP", "side": side, "setup": "BOS_RETRACE", "timeframe": "15m",
            "entry": entry, "stop": stop, "initial_stop": stop, "tp": tp, "risk": 1,
            "placed_ts": placed, "expires_ts": expiry or placed+3_600_000,
            "last_bar": 0, "status": "PENDING", "score": 80, "net_rr": 1.9,
            "zone_created_ts": placed-900_000, "stop_distance_pct": 1,
            "invalidation": stop, "evidence": ["fixture structure"], "parent_keys": []}


def state_with_order(side=1, placed=NOW, expiry=None):
    cfg = b.Config()
    s = b.new_state(cfg)
    s["pending"] = order(side, placed, expiry)
    s["last_run_ts"] = placed
    return s


def lifecycle_snapshot(bars, now=None):
    s = snapshot(now or bars[-1].ts+b.TF["3m"])
    s.candles["3m"] = bars
    return s


def analysis(plans=None, ctx=None):
    ctx = ctx or {"1H": {"side": 1}, "4H": {"side": 1}, "15m": {"side": 1}}
    return {"health": [], "contexts": ctx, "plans": plans or [], "watch": [], "rejections": {}}






class DataTests(unittest.TestCase):
    def test_open_candles_and_future_closed_candles_excluded(self):
        d = {"now": NOW, "price": 100, "trusted": True, "candles": {"3m": [
            dict(vars(candle(NOW-180_000))),
            dict(vars(candle(NOW, confirmed=True))),
            dict(vars(candle(NOW-360_000, confirmed=False))),
        ]}}
        self.assertEqual([c.ts for c in b.Snapshot.parse(d).candles["3m"]], [NOW-180_000])

    def test_string_zero_confirm_is_not_true(self):
        row = dict(vars(candle(NOW-180_000)))
        row["confirmed"] = "0"
        self.assertFalse(b.Candle.parse(row).confirmed)

    def test_invalid_ohlc_and_nan_rejected(self):
        for changes in ({"high": 90}, {"close": float("nan")}, {"volume": -1}):
            row = dict(vars(candle(NOW)))
            row.update(changes)
            with self.assertRaises(ValueError):
                b.Candle.parse(row)

    def test_pivots_require_right_hand_confirmation(self):
        rows = [candle(NOW+i*180_000, 100, h, 99, 100) for i, h in enumerate([101, 102, 105, 103, 102])]
        self.assertFalse(b.pivots(rows[:4]))
        self.assertEqual(b.pivots(rows)[0]["known_ts"], rows[4].ts)

    def test_health_detects_missing_bar(self):
        s = snapshot()
        s.candles["15m"].pop(-3)
        self.assertIn("GAP_15m", b.health(s, b.Config()))

    def test_tick_rounding_handles_nondecimal_tick(self):
        self.assertEqual(b.round_tick(100.13, .25), 100)
        self.assertEqual(b.round_tick(100.13, .25, True), 100.25)

    def test_bad_config_rejected(self):
        with self.assertRaises(ValueError):
            replace(b.Config(), min_net_rr=3).validate()


class ExecutionTests(unittest.TestCase):
    def test_partial_cancellation_retains_unknown_fill_interval(self):
        s = state_with_order()
        b.request_cancel(s, s["pending"], "HTF_FLIP", NOW+60_000)
        self.assertIsNotNone(s["pending"])
        b.advance_lifecycle(s, lifecycle_snapshot([candle(NOW, l=99.9)]), b.Config())
        self.assertEqual(s["orders"][0]["status"], "UNRESOLVED")
        self.assertEqual(s["reconciliation"]["reason"], "CANCEL_INSIDE_TOUCHED_BAR")

    def test_fill_is_at_limit_not_candle_close(self):
        s = state_with_order()
        c = candle(NOW, 101, 101.5, 99.9, 100.8)
        b.advance_lifecycle(s, lifecycle_snapshot([c]), b.Config())
        self.assertEqual(s["active"]["entry"], 100)
        self.assertEqual(s["orders"][0]["status"], "FILLED")

    def test_exact_touch_does_not_assume_fill(self):
        s = state_with_order()
        b.advance_lifecycle(s, lifecycle_snapshot([candle(NOW, l=100)]), b.Config())
        self.assertIsNone(s["active"])
        self.assertIsNotNone(s["pending"])

    def test_fill_bar_stop_is_not_skipped(self):
        s = state_with_order()
        b.advance_lifecycle(s, lifecycle_snapshot([candle(NOW, 101, 103, 98, 100)]), b.Config())
        self.assertIsNone(s["active"])
        self.assertLess(s["trades"][0]["net_r"], -1)
        self.assertTrue(s["trades"][0]["ambiguous_ohlc"])

    def test_fill_bar_pre_entry_high_cannot_be_profit(self):
        s = state_with_order()
        b.advance_lifecycle(s, lifecycle_snapshot([candle(NOW, 101, 103, 99.9, 100.5)]), b.Config())
        self.assertIsNotNone(s["active"])
        self.assertFalse(s["trades"])
        self.assertEqual(s["active"]["mfe_r"], 0)

    def test_fill_bar_close_beyond_target_proves_traversal(self):
        s = state_with_order()
        b.advance_lifecycle(s, lifecycle_snapshot([candle(NOW, 101, 103, 99.9, 102.5)]), b.Config())
        self.assertEqual(s["trades"][0]["close_reason"], "TAKE_PROFIT")

    def test_same_bar_stop_and_target_stop_wins(self):
        s = state_with_order()
        bars = [candle(NOW, 101, 101.5, 99.9, 100.8),
                candle(NOW+180_000, 100.8, 103, 98.5, 102.5)]
        b.advance_lifecycle(s, lifecycle_snapshot(bars), b.Config())
        self.assertEqual(s["trades"][0]["close_reason"], "STOP")

    def test_short_execution_symmetric(self):
        s = state_with_order(-1)
        bars = [candle(NOW, 99, 100.1, 98.8, 99.5),
                candle(NOW+180_000, 99.5, 100, 97.5, 98)]
        b.advance_lifecycle(s, lifecycle_snapshot(bars), b.Config())
        self.assertEqual(s["trades"][0]["close_reason"], "TAKE_PROFIT")
        self.assertGreater(s["trades"][0]["net_r"], 1.8)

    def test_stop_gap_uses_worse_open_price(self):
        s = state_with_order()
        bars = [candle(NOW, 101, 101.5, 99.9, 100.8),
                candle(NOW+180_000, 97, 98, 96, 97)]
        b.advance_lifecycle(s, lifecycle_snapshot(bars), b.Config())
        self.assertEqual(s["trades"][0]["close_reason"], "STOP_GAP")
        self.assertLess(s["trades"][0]["net_r"], -3)

    def test_fill_after_expiry_is_impossible(self):
        s = state_with_order(expiry=NOW+180_000)
        bars = [candle(NOW), candle(NOW+180_000, l=99.9)]
        b.advance_lifecycle(s, lifecycle_snapshot(bars), b.Config())
        self.assertEqual(s["orders"][0]["status"], "EXPIRED")
        self.assertIsNone(s["active"])

    def test_pre_expiry_fill_survives_late_report(self):
        s = state_with_order(expiry=NOW+360_000)
        bars = [candle(NOW, l=99.9), candle(NOW+180_000), candle(NOW+360_000)]
        b.advance_lifecycle(s, lifecycle_snapshot(bars), b.Config())
        self.assertEqual(s["orders"][0]["status"], "FILLED")

    def test_partial_expiry_bar_is_unresolved_and_blocks(self):
        s = state_with_order(expiry=NOW+90_000)
        b.advance_lifecycle(s, lifecycle_snapshot([candle(NOW, l=99.9)]), b.Config())
        self.assertEqual(s["orders"][0]["status"], "UNRESOLVED")
        self.assertIsNotNone(s["reconciliation"])
        out = b.run_cycle(s, snapshot(NOW+900_000), b.Config())
        self.assertEqual(out["action"], "RECONCILIATION_REQUIRED")

    def test_partial_placement_bar_cannot_invent_fill(self):
        s = state_with_order(placed=NOW+60_000)
        b.advance_lifecycle(s, lifecycle_snapshot([candle(NOW, l=99.9)]), b.Config())
        self.assertEqual(s["orders"][0]["status"], "UNRESOLVED")

    def test_no_repeated_pnl_on_restart(self):
        s = state_with_order()
        bars = [candle(NOW, 101, 103, 98, 100)]
        snap = lifecycle_snapshot(bars)
        b.advance_lifecycle(s, snap, b.Config())
        s = json.loads(json.dumps(s))
        b.advance_lifecycle(s, snap, b.Config())
        self.assertEqual(len(s["trades"]), 1)
        self.assertEqual(len(s["orders"]), 1)

    def test_stop_not_moved_retroactively_between_reports(self):
        s = state_with_order()
        bars = [candle(NOW, 101, 101.5, 99.9, 100.8),
                candle(NOW+180_000, 100.8, 101.7, 100.5, 101.3),
                candle(NOW+360_000, 101.3, 101.8, 100.5, 101.4),
                candle(NOW+540_000, 101.4, 101.8, 99.5, 100.7)]
        b.advance_lifecycle(s, lifecycle_snapshot(bars), b.Config())
        self.assertIsNotNone(s["active"])
        self.assertEqual(s["active"]["stop"], 99)
        self.assertFalse(any(e["kind"] == "PAPER_STOP_AMENDED" for e in s["events"]))

    def test_stop_amendment_effective_after_report(self):
        s = state_with_order()
        bars = [candle(NOW, 101, 101.5, 99.9, 100.8),
                candle(NOW+180_000, 100.8, 101.7, 100.5, 101.3),
                candle(NOW+360_000, 101.3, 101.8, 100.5, 101.4)]
        snap = lifecycle_snapshot(bars)
        snap.price = 101.4
        b.advance_lifecycle(s, snap, b.Config())
        b.amend_at_report(s, snap, b.Config())
        self.assertEqual(s["active"]["stop"], 99)
        be = s["active"]["pending_stop"]["price"]
        self.assertGreater(be, 100)
        b.manage_bar(s, candle(NOW+540_000, 101.4, 101.8, 99.9, 100.8), b.Config())
        self.assertEqual(s["trades"][0]["close_reason"], "STOP")
        self.assertGreaterEqual(s["trades"][0]["net_r"], 0)

    def test_intraday_deadline_closes_without_setup_conflict_exit(self):
        cfg = replace(b.Config(), max_hold_minutes=3)
        s = state_with_order()
        b.advance_lifecycle(s, lifecycle_snapshot([candle(NOW, l=99.9)]), cfg)
        self.assertEqual(s["trades"][0]["close_reason"], "INTRADAY_TIME_EXIT")


class RouterTests(unittest.TestCase):
    def test_direction_conflict_cannot_place_two_orders(self):
        cfg = b.Config()
        s = b.new_state(cfg)
        p, q = order(), order(-1)
        q["id"] = "opposite"
        with patch.object(b, "analyze", return_value=analysis([p, q])):
            out = b.run_cycle(s, snapshot(), cfg)
        self.assertEqual(out["action"], "DIRECTION_CONFLICT")
        self.assertIsNone(s["pending"])

    def test_pending_plan_persists_despite_better_new_setup(self):
        s = state_with_order()
        snap = snapshot(NOW+900_000, price=101)
        snap.candles["3m"] = [candle(NOW+i*180_000) for i in range(5)]
        with patch.object(b, "health", return_value=[]), patch.object(b, "analyze", return_value=analysis([order(-1)])):
            out = b.run_cycle(s, snap, b.Config())
        self.assertEqual(out["action"], "WAIT_LIMIT")
        self.assertEqual(s["pending"]["side"], 1)

    def test_current_bias_cannot_cancel_a_historical_fill(self):
        s = state_with_order()
        snap = snapshot(NOW+900_000, price=101)
        snap.candles["3m"] = [candle(NOW+i*180_000, h=101.5, l=99.9 if i == 0 else 100.5) for i in range(5)]
        ctx = {tf: {"side": -1} for tf in ("1H", "4H", "15m")}
        with patch.object(b, "health", return_value=[]), patch.object(b, "analyze", return_value=analysis(ctx=ctx)):
            out = b.run_cycle(s, snap, b.Config())
        self.assertEqual(out["action"], "FOLLOW")
        self.assertEqual(s["orders"][0]["status"], "FILLED")

    def test_missing_lifecycle_history_freezes_checkpoint(self):
        s = state_with_order()
        before = copy.deepcopy(s)
        snap = snapshot(NOW+900_000)
        snap.candles["3m"] = [candle(NOW+180_000)]
        out = b.run_cycle(s, snap, b.Config())
        self.assertEqual(out["action"], "DATA_HOLD")
        self.assertEqual(s, before)

    def test_duplicate_report_is_idempotent(self):
        s = state_with_order()
        before = copy.deepcopy(s)
        out = b.run_cycle(s, snapshot(), b.Config())
        self.assertEqual(out["action"], "DUPLICATE_OR_OUT_OF_ORDER")
        self.assertEqual(s, before)

    def test_daily_losses_not_offset_by_winners(self):
        cfg = b.Config()
        s = b.new_state(cfg)
        s["trades"] = [{"closed_ts": NOW, "net_r": x} for x in (4, -1.1, -1.1, -1.1)]
        with patch.object(b, "analyze", return_value=analysis([order()])):
            out = b.run_cycle(s, snapshot(), cfg)
        self.assertEqual(out["action"], "DAILY_PAUSE")

    def test_does_not_resize_or_expose_deposit_allocation(self):
        s = state_with_order()
        msg = b.build_message(s, snapshot(), {"action": "WAIT_LIMIT", "analysis": {}}, b.Config())
        self.assertNotIn("депозит", msg)
        self.assertNotIn("розмір позиції", msg)
        self.assertIn("SL:", msg)
        self.assertIn("PAPER", msg)


class PersistenceTests(unittest.TestCase):
    def test_corrupted_state_never_resets_silently(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/"state.json"
            path.write_text("{broken")
            with self.assertRaises(ValueError):
                b.load_state(path, b.Config())

    def test_legacy_state_preserved_and_requires_new_path(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/"state.json"
            data = {"active_trade": {"id": "legacy"}, "anchors_v10": []}
            path.write_text(json.dumps(data))
            with self.assertRaises(ValueError):
                b.load_state(path, b.Config())
            self.assertEqual(json.loads(path.read_text()), data)

    def test_journal_keeps_legacy_history_and_exports_idempotently(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/"journal.json"
            data = {"trades": [{"id": "old"}], "custom_key": [1, 2]}
            path.write_text(json.dumps(data))
            state = b.new_state(b.Config())
            state["last_run_ts"] = NOW
            state["trades"] = [{"id": "new", "net_r": 1}]
            b.export_journal(path, state)
            b.export_journal(path, state)
            out = json.loads(path.read_text())
            self.assertEqual(out["trades"][:1], data["trades"])
            self.assertEqual(out["custom_key"], [1, 2])
            self.assertEqual(len(out["full_v12"]["trades"]), 1)

    def test_configuration_change_with_open_order_blocked(self):
        s = state_with_order()
        with self.assertRaises(ValueError):
            b.run_cycle(s, snapshot(NOW+900_000), replace(b.Config(), tick=.1))


def full_fixture(side="LONG"):
    cfg = replace(b.Config(), core=core)
    with b.market_clock(core, NOW):
        ctx, anchor, data = core._synthetic_context(side, reaction=False, distance_atr=.8, runway_atr=8)
    anchor.invalidation = anchor.level-core.side_sign(side)*.7*ctx["atr15"]
    anchor.score = 85
    s = b.Snapshot.parse({"now":NOW, "price":data["price"], "bid":data["price"]-.005,
                          "ask":data["price"]+.005, "trusted":True, "ticker_ts":NOW,
                          "instrument":cfg.instrument,
                          "candles":{tf:[asdict(c) for c in rows] for tf,rows in data["candles"].items()},
                          "smt_candles_15m":[asdict(c) for c in data.get("smt_candles_15m",[])]})
    return cfg, ctx, anchor, s


class FullSetupTests(unittest.TestCase):
    def test_neutral_direction_is_zero_not_short(self):
        self.assertEqual(core.side_sign('LONG'),1)
        self.assertEqual(core.side_sign('SHORT'),-1)
        self.assertEqual(core.side_sign('NEUTRAL'),0)
        self.assertEqual(core.side_sign(''),0)

    def test_neutral_htf_matches_plan_and_report(self):
        cfg,ctx,a,s = full_fixture()
        out = b.run_cycle(b.new_state(cfg),s,cfg)
        p = out['selected']
        self.assertEqual(p['htf_1h'],out['analysis']['contexts']['1H']['side'])
        self.assertEqual(p['htf_4h'],out['analysis']['contexts']['4H']['side'])
        self.assertEqual(p['htf_1h'],0)
        self.assertEqual(p['htf_4h'],0)

    def test_unknown_anchor_direction_cannot_make_short_limit(self):
        cfg,ctx,a,s = full_fixture()
        a.side = 'NEUTRAL'
        self.assertEqual(b.full_plan(a,ctx,s,cfg,{})[1],'INVALID_DIRECTION_OR_VOLATILITY')

    def test_all_24_detector_bodies_match_original_manifest(self):
        manifest = json.loads((b.ROOT/"setup_preservation_manifest.json").read_text())
        nodes = {n.name:n for n in ast.parse(Path(core.__file__).read_text()).body if isinstance(n,ast.FunctionDef)}
        self.assertEqual(manifest["setups_count"],24)
        self.assertEqual([r["name"] for r in manifest["detectors"]],[d.__name__ for d in core.DETECTORS])
        self.assertEqual(len(core.DETECTED_SETUP_TYPES),24)
        all_symbols = {n.name for n in ast.parse(Path(core.__file__).read_text()).body
                       if isinstance(n,(ast.FunctionDef,ast.ClassDef))}
        self.assertTrue(set(manifest["original_symbols"])<=all_symbols)
        for row in manifest["detectors"]:
            with self.subTest(detector=row["name"]):
                actual = hashlib.sha256(preservation_ast_dump(nodes[row["name"]]).encode()).hexdigest()
                self.assertEqual(actual,row["ast_sha256"])

    def test_preservation_hash_handles_python_311_ast_without_type_params(self):
        manifest = json.loads((b.ROOT/'setup_preservation_manifest.json').read_text())
        nodes = {n.name:n for n in ast.parse(Path(core.__file__).read_text()).body if isinstance(n,ast.FunctionDef)}
        for row in manifest['detectors']:
            with self.subTest(detector=row['name']):
                node = copy.deepcopy(nodes[row['name']])
                for item in ast.walk(node):
                    if 'type_params' in item._fields:
                        item._fields = tuple(f for f in item._fields if f != 'type_params')
                        if hasattr(item,'type_params'):
                            del item.type_params
                actual = hashlib.sha256(preservation_ast_dump(node).encode()).hexdigest()
                self.assertEqual(actual,row['ast_sha256'])

    def test_preservation_hash_still_detects_real_setup_change(self):
        manifest = json.loads((b.ROOT/'setup_preservation_manifest.json').read_text())
        row = manifest['detectors'][0]
        node = next(n for n in ast.parse(Path(core.__file__).read_text()).body
                    if isinstance(n,ast.FunctionDef) and n.name==row['name'])
        number = next(n for n in ast.walk(node) if isinstance(n,ast.Constant) and type(n.value) in (int,float))
        number.value += 1
        actual = hashlib.sha256(preservation_ast_dump(node).encode()).hexdigest()
        self.assertNotEqual(actual,row['ast_sha256'])

    def test_all_detectors_actually_called_in_new_production_cycle(self):
        cfg,ctx,a,s = full_fixture()
        calls, wrapped = [], []
        for detector in core.DETECTORS:
            @wraps(detector)
            def invoke(context, original=detector):
                calls.append(original.__name__)
                return original(context)
            wrapped.append(invoke)
        with patch.object(core,"DETECTORS",wrapped):
            out = b.run_cycle(b.new_state(cfg),s,cfg)
        self.assertEqual(calls,[d.__name__ for d in core.DETECTORS])
        self.assertEqual(out["analysis"]["registered_setups"],24)
        self.assertFalse([d for d in out["analysis"]["detectors"] if d["status"]=="ERROR"])
        self.assertEqual(out["action"],"PLACE_LIMIT")

    def test_incomplete_registry_cannot_run_silently(self):
        cfg,ctx,a,s = full_fixture()
        with patch.object(core,"DETECTORS",core.DETECTORS[:2]),self.assertRaises(ValueError):
            b.analyze(s,cfg,b.new_state(cfg))

    def test_all_24_setup_types_can_reach_common_limit_planner(self):
        for setup in sorted(core.DETECTED_SETUP_TYPES):
            with self.subTest(setup=setup):
                side = "SHORT" if setup.endswith("SHORT") else "LONG"
                cfg,ctx,a,s = full_fixture(side)
                a.setup_type = setup
                a.setup_family = core.journal_setup_family(setup)
                p,why = b.full_plan(a,ctx,s,cfg,{})
                self.assertIsNotNone(p,why)
                self.assertEqual(p["setup_type"],setup)
                self.assertEqual(p["execution_source"],"LIMIT_ARMED_AT_LEVEL")
                self.assertEqual(p["entry_stage"],"CORE")

    def test_limit_geometry_is_anchor_not_ticker(self):
        cfg,ctx,a,s = full_fixture()
        p,why = b.full_plan(a,ctx,s,cfg,{})
        self.assertEqual(p["entry"],b.round_tick(a.level,cfg.tick))
        self.assertLess(p["stop"],a.invalidation)
        self.assertGreaterEqual(p["net_rr"],cfg.min_net_rr)
        self.assertLessEqual(p["rr1"],2+1e-8)

    def test_visited_level_and_crossing_spread_rejected(self):
        cfg,ctx,a,s = full_fixture()
        s.candles["3m"].append(candle(NOW,a.level+1,a.level+2,a.level-.01,a.level+1))
        self.assertEqual(b.full_plan(a,ctx,s,cfg,{})[1],"LEVEL_ALREADY_VISITED_AFTER_OBSERVATION")
        a.level = s.ask
        self.assertEqual(b.full_plan(a,ctx,s,cfg,{})[1],"LIMIT_WOULD_CROSS_SPREAD")

    def test_mixed_htf_is_not_blanket_veto(self):
        cfg,ctx,a,s = full_fixture()
        targets = [{"kind":"test","level":a.level+3,"distance":3}]
        directions = iter(["LONG","SHORT"])
        with patch.object(core,"structure_snapshot",side_effect=lambda *args:{"direction":next(directions)}), \
                patch.object(b,"full_targets",return_value=targets):
            p,why = b.full_plan(a,ctx,s,cfg,{})
        self.assertIsNotNone(p,why)

    def test_both_htf_against_requires_original_reversal_and_ltf_confirmation(self):
        cfg,ctx,a,s = full_fixture()
        a.setup_type = "BREAKOUT_RETEST"
        with patch.object(core,"structure_snapshot",return_value={"direction":"SHORT"}):
            self.assertEqual(b.full_plan(a,ctx,s,cfg,{})[1],"BOTH_HTF_AGAINST_WITHOUT_CONFIRMED_REVERSAL")
            a.setup_type = "SWEEP_RECLAIM"
            ctx["structure15"]["direction"] = ctx["structure3m"]["direction"] = "LONG"
            targets = [{"kind":"test","level":a.level+3,"distance":3}]
            with patch.object(b,"full_targets",return_value=targets):
                p,why = b.full_plan(a,ctx,s,cfg,{})
            self.assertIsNotNone(p,why)

    def test_position_allocation_absent_from_plan_and_message(self):
        cfg,ctx,a,s = full_fixture()
        state = b.new_state(cfg)
        out = b.run_cycle(state,s,cfg)
        p = state["pending"]
        for key in ("qty","quantity","position_size","position_risk_pct","deposit","margin","leverage"):
            self.assertNotIn(key,p)
        msg = b.build_message(state,s,out,cfg).lower()
        self.assertNotIn("розмір позиції",msg)
        self.assertNotIn("депозит",msg)

    def test_repeated_detection_cannot_refresh_anchor_age(self):
        cfg,ctx,a,s = full_fixture()
        state = b.new_state(cfg)
        first = b.merge_full_anchors([copy.deepcopy(a)],state,s,cfg)[0]
        later = copy.deepcopy(a)
        later.id = "another-id"
        later.created_ts += 900_000
        later.expires_ts += 900_000
        s.now += 900_000
        second = b.merge_full_anchors([later],state,s,cfg)[0]
        self.assertEqual((first.id,first.created_ts,first.expires_ts),(second.id,second.created_ts,second.expires_ts))

    def test_anchor_memory_does_not_truncate_to_twelve(self):
        cfg,ctx,a,s = full_fixture()
        anchors = []
        for i,setup in enumerate(sorted(core.DETECTED_SETUP_TYPES)):
            clone = copy.deepcopy(a)
            clone.id,clone.setup_type = str(i),setup
            anchors.append(clone)
        self.assertEqual(len(b.merge_full_anchors(anchors,b.new_state(cfg),s,cfg)),24)

    def test_statistics_do_not_mix_old_architecture_or_ambiguous_fills(self):
        cfg,ctx,a,s = full_fixture()
        state = b.new_state(cfg)
        state["trades"] = [{"setup_type":a.setup_type,"bot_version_at_entry":"v10","net_r":-1} for _ in range(80)]
        state["trades"] += [{"setup_type":a.setup_type,"bot_version_at_entry":b.VERSION,"net_r":-1,"ambiguous_ohlc":True} for _ in range(30)]
        stat = b.setup_statistics(state,cfg)[a.setup_type]
        self.assertEqual(stat["trades"],0)
        self.assertEqual(stat["status"],"INSUFFICIENT_SAMPLE")
        self.assertFalse(stat["probability_validated"])

    def test_confirmed_15m_resample_uses_four_and_sixteen_bars(self):
        rows = [candle(NOW+i*900_000) for i in range(16)]
        hourly = core.resample_candles(rows,60)
        fourhour = core.resample_candles(rows,240)
        self.assertEqual(len(hourly),4)
        self.assertTrue(all(c.confirmed and c.volume==400 for c in hourly))
        self.assertEqual(len(fourhour),1)
        self.assertTrue(fourhour[0].confirmed)
        self.assertEqual(fourhour[0].volume,1600)

    def test_resample_missing_or_unconfirmed_source_cannot_confirm_htf(self):
        rows = [candle(NOW+i*900_000) for i in range(4)]
        self.assertFalse(core.resample_candles(rows[:3],60)[0].confirmed)
        self.assertFalse(core.resample_candles(rows[1:],60)[0].confirmed)
        rows[2] = replace(rows[2],confirmed=False)
        self.assertFalse(core.resample_candles(rows,60)[0].confirmed)

    def test_resample_duplicate_source_does_not_inflate_volume(self):
        rows = [candle(NOW+i*900_000) for i in range(4)]
        self.assertEqual(core.resample_candles(rows+[rows[-1]],60)[0].volume,400)

    def test_snapshot_clock_restored_after_exception(self):
        clock = core.now_utc
        with self.assertRaises(RuntimeError),b.market_clock(core,NOW):
            self.assertEqual(int(core.now_utc().timestamp()*1000),NOW)
            raise RuntimeError("fixture")
        self.assertIs(core.now_utc,clock)

    def test_smt_misaligned_peer_cannot_confirm_setup(self):
        cfg,ctx,a,s = full_fixture()
        s.smt_candles = [replace(c,ts=c.ts-900_000) for c in s.candles["15m"]]
        with b.market_clock(core,NOW):
            actual = b.build_full_context(s,b.new_state(cfg),cfg)
        self.assertFalse(actual["smt"]["available"])
        self.assertEqual(actual["smt"]["data_status"],"UNAVAILABLE_OR_MISALIGNED")

    def test_smt_matching_closed_intervals_preserved(self):
        cfg,ctx,a,s = full_fixture()
        s.smt_candles = list(s.candles["15m"])
        with b.market_clock(core,NOW):
            actual = b.build_full_context(s,b.new_state(cfg),cfg)
        self.assertTrue(actual["smt"]["available"])
        self.assertEqual(actual["smt"]["smt_bars"],30)


class FullLifecycleTests(unittest.TestCase):
    def test_target_already_beyond_ticker_at_placement_cannot_cancel_retracement_plan(self):
        state = state_with_order()
        state['pending']['target_cancel_armed'] = False
        with patch.object(b,'analyze',return_value=analysis()):
            out = b.run_cycle(state,snapshot(NOW+900_000,price=103),b.Config())
        self.assertEqual(out['action'],'WAIT_LIMIT')
        self.assertIsNotNone(state['pending'])

    def test_target_cancellation_arms_only_after_price_is_before_target(self):
        state = state_with_order()
        state['pending']['target_cancel_armed'] = False
        with patch.object(b,'analyze',return_value=analysis()):
            b.run_cycle(state,snapshot(NOW+900_000,price=101),b.Config())
            self.assertTrue(state['pending']['target_cancel_armed'])
            b.run_cycle(state,snapshot(NOW+1800_000,price=103),b.Config())
        self.assertIsNone(state['pending'])
        self.assertEqual(state['orders'][0]['reason'],'TARGET_REACHED_WITHOUT_FILL')

    def test_partial_tp_then_stop_accounts_for_remaining_only_and_all_fees(self):
        state = state_with_order()
        state["pending"].update(tp1=102,tp2=103,tp3=104,partials={"TP1":.65,"TP2":.2,"TP3":.15})
        bars = [candle(NOW,101,101.5,99.9,100.8),candle(NOW+180_000,100.8,102.5,100,102),
                candle(NOW+360_000,102,102,98.5,99)]
        cfg = b.Config()
        b.advance_lifecycle(state,lifecycle_snapshot(bars),cfg)
        t = state["trades"][0]
        tp_exit,stop_exit = 102*(1-.0002),99*(1-.0002)
        gross = .65*(tp_exit-100)+.35*(stop_exit-100)
        fees = 100*.0002+.65*tp_exit*.0005+.35*stop_exit*.0005
        self.assertAlmostEqual(t["net_r"],gross-fees)
        self.assertEqual(len(t["realized_legs"]),1)
        self.assertGreater(t["net_r"],0)

    def test_all_three_targets_close_once_without_double_fee(self):
        state = state_with_order()
        state["pending"].update(tp1=102,tp2=103,tp3=104,partials={"TP1":.65,"TP2":.2,"TP3":.15})
        bars = [candle(NOW,101,101.5,99.9,100.8),candle(NOW+180_000,100.8,105,100,104)]
        b.advance_lifecycle(state,lifecycle_snapshot(bars),b.Config())
        t = state["trades"][0]
        self.assertEqual(len(t["realized_legs"]),3)
        self.assertEqual(len(state["trades"]),1)
        expected = sum(f*(p*.9998-100) for f,p in ((.65,102),(.2,103),(.15,104)))
        fees = .02+sum(f*p*.9998*.0005 for f,p in ((.65,102),(.2,103),(.15,104)))
        self.assertAlmostEqual(t["net_r"],expected-fees)

    def test_one_full_tick_required_for_paper_fill(self):
        state = state_with_order()
        b.advance_lifecycle(state,lifecycle_snapshot([candle(NOW,l=99.9995)]),b.Config())
        self.assertIsNone(state["active"])
        self.assertIsNotNone(state["pending"])

    def test_no_probe_no_followthrough_closure_in_new_lifecycle(self):
        state = state_with_order()
        bars = [candle(NOW,101,101.5,99.9,100.8)]
        bars += [candle(NOW+i*180_000,100.8,101,100.2,100.5) for i in range(1,15)]
        b.advance_lifecycle(state,lifecycle_snapshot(bars),b.Config())
        self.assertIsNotNone(state["active"])
        self.assertFalse(state["trades"])

    def test_clean_real_cycle_places_plan_then_duplicate_does_not_repeat(self):
        cfg,ctx,a,s = full_fixture()
        state = b.new_state(cfg)
        self.assertEqual(b.run_cycle(state,s,cfg)["action"],"PLACE_LIMIT")
        before = copy.deepcopy(state)
        self.assertEqual(b.run_cycle(state,s,cfg)["action"],"DUPLICATE_OR_OUT_OF_ORDER")
        self.assertEqual(state,before)

    def test_original_active_position_preserves_levels_and_completed_partials(self):
        cfg,ctx,a,s = full_fixture()
        raw = {"version":"organic_v10.6.1","active_trade":{"id":"old-position","side":"LONG",
               "entry":100,"stop_initial":99,"stop_current":100.2,"tp1":102,"tp2":103,"tp3":104,
               "setup_type":"BREAKOUT_RETEST","opened_at":b.iso(NOW-900_000),"last_checked_3m_ts":NOW-180_000,
               "tp1_hit":True,"tp1_size_pct":.65,"tp2_size_pct":.2,"tp3_runner_pct":.15,
               "bot_version_at_entry":"organic_v10.6.1"},"anchors_v10":[]}
        migrated = b.migrate_state(raw,cfg)
        p = migrated["active"]
        self.assertEqual((p["id"],p["entry"],p["initial_stop"],p["stop"],p["tp2"]),("old-position",100,99,100.2,103))
        self.assertAlmostEqual(p["remaining"],.35)
        self.assertEqual(p["entry_version"],"organic_v10.6.1")
        self.assertEqual(migrated["legacy_snapshot"],raw)

    def test_original_pending_order_preserves_levels_and_expiry(self):
        cfg,ctx,a,s = full_fixture()
        raw = {"anchors_v10":[],"pending_limit_v10":[{"order_id":"old-limit","side":"LONG",
               "limit_price":100,"plan":{"entry":100,"stop":99,"tp1":102,"tp2":103,"tp3":104},
               "setup_type":"BREAKOUT_RETEST","placed_ts":NOW,"expires_ts":NOW+1800_000}]}
        migrated = b.migrate_state(raw,cfg)
        self.assertEqual(migrated["pending"]["id"],"old-limit")
        self.assertEqual(migrated["pending"]["expires_ts"],NOW+1800_000)
        self.assertIsNone(migrated["reconciliation"])

    def test_multiple_original_pending_orders_are_not_silently_deleted(self):
        cfg,ctx,a,s = full_fixture()
        raw = {"anchors_v10":[],"pending_limit_v10":[{"order_id":"a"},{"order_id":"b"}]}
        migrated = b.migrate_state(raw,cfg)
        self.assertEqual(migrated["legacy_pending_orders"],raw["pending_limit_v10"])
        self.assertIsNotNone(migrated["reconciliation"])

    def test_previous_small_v11_pending_plan_is_preserved_until_resolved(self):
        cfg,ctx,a,s = full_fixture()
        raw = state_with_order()
        raw["schema"],raw["version"] = "ict_limit_state_v11","ict-limit-v11.0.0"
        migrated = b.migrate_state(raw,cfg)
        self.assertEqual(migrated["pending"]["entry"],100)
        self.assertEqual(migrated["pending"]["stop"],99)
        self.assertEqual(migrated["pending"]["entry_version"],"ict-limit-v11.0.0")
        self.assertEqual(migrated["legacy_snapshot"],raw)

    def test_replay_higher_timeframes_and_smt_never_expose_future_bars(self):
        cfg,ctx,a,s = full_fixture()
        rows = {tf:[asdict(c) for c in cs] for tf,cs in s.candles.items()}
        rows["3m"] += [asdict(candle(NOW+i*180_000)) for i in range(5)]
        rows["1H"].append(asdict(candle(NOW)))
        smt = [asdict(c) for c in s.candles["15m"]]+[asdict(candle(NOW))]
        seen = []
        def inspect(state,snap,config):
            seen.append(snap)
            for tf,cs in snap.candles.items():
                self.assertTrue(all(c.ts+b.TF[tf]<=snap.now for c in cs))
            self.assertTrue(all(c.ts+b.TF['15m']<=snap.now for c in snap.smt_candles))
            return {'action':'WATCH'}
        with patch.object(b,'run_cycle',side_effect=inspect):
            b.replay({'candles':rows,'smt_candles_15m':smt,'start_ts':NOW,'end_ts':NOW+900_000},cfg)
        self.assertEqual(len(seen),2)
        self.assertEqual(seen[0].candles['1H'][-1].ts,NOW-3_600_000)
        self.assertEqual(seen[0].smt_candles[-1].ts,NOW-900_000)

    def test_export_updates_original_analytics_and_preserves_seventy_trades(self):
        cfg,ctx,a,s = full_fixture()
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/"journal.json"
            history = [{"id":f"old{i}","net_r":-1,"result":"LOSS"} for i in range(70)]
            b.atomic_write(path,{"trades":history,"custom_field":{"keep":True}})
            state = b.new_state(cfg)
            state["last_run_ts"] = NOW
            state["trades"] = [{"id":"new","net_r":2,"result":"WIN","closed_at":b.iso(NOW)}]
            b.export_journal(path,state,core)
            b.export_journal(path,state,core)
            journal = b.read_json(path)
            self.assertEqual(journal["trades"][:70],history)
            self.assertEqual(len(journal["trades"]),71)
            self.assertEqual(journal["analytics"]["net_r"],-68)
            self.assertEqual(journal["custom_field"],{"keep":True})


if __name__ == "__main__":
    unittest.main()
