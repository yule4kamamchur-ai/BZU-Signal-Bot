"""Chronological paper execution (v15.2).
Supports:
- Causal M1 bar processing (stop updates effective AFTER the processed bar)
- Two-stage TP1 (50% partial lock + auto-BE) + Runner (50% to TP2 / structural trail)
- Alpha-Decay Time-Stop (tighten SL at 45m, early exit at 60m if stalled without impulse)
"""
from __future__ import annotations
import math
from mexc_geometry import POLICY, compute_net_be_price, compute_tp1_price, stale_tighten_stop, structural_trail


def advance(active, item, now, *, round_price, legacy_cursor=0):
    side = int(active['side'])
    entry = float(active['entry'])
    risk = float(active['risk'])
    tp = float(active['tp'])
    stop = float(active['stop'])
    tick = float(item.get('tick') or active.get('tick') or .001)
    pending = active.get('phase') == 'PENDING_LIMIT'
    start = int(active.get('placed_ts' if pending else 'opened_ts', now))
    if 'processed_until_ts' not in active:
        active['processed_until_ts'] = max(start, int(legacy_cursor or start))
        active['quality'] = 'LEGACY_CURSOR_MIGRATION'
    cursor = int(active['processed_until_ts'])
    peak = float(active.get('peak_r', 0.))
    quality = active.get('quality', 'OBSERVED_M1')
    if cursor > now:
        return None
    m1 = 'candles_1m' in item
    step = 60000 if m1 else 300000
    if not m1:
        quality = 'LEGACY_M5'
    bars = {int(c['t']) * 1000: c for c in (item.get('candles_1m') if m1 else item.get('candles_5m') or []) or []}
    quote = float(item.get('bid' if side == 1 else 'ask') or item.get('price') or entry)
    quote_ts = int(item.get('quote_ts', now))
    quote_ok = item.get('quote_valid', not m1) and 0 <= now - quote_ts <= 60000 and quote_ts >= start and quote > 0
    expires = int(active.get('expires_ts', start + 8 * 60000))

    tp1_enabled = bool(active.get('tp1_enabled', False))
    tp1_r = float(active.get('tp1_r', 1.0))
    tp1_price = float(active.get('tp1') or compute_tp1_price(entry, risk, side, tick, tp1_r=tp1_r)) if tp1_enabled else None
    partial_pct = float(active.get('partial_pct', 0.50))
    time_stop_enabled = bool(active.get('time_stop_enabled', False))
    stale_tighten_min = int(active.get('stale_tighten_min', 45))
    stale_exit_min = int(active.get('stale_exit_min', 60))

    def _exit_reason(base_reason):
        if active.get('tp1_hit'):
            if base_reason == 'TAKE_PROFIT':
                return 'TP1_RUNNER_TP'
            if base_reason in ('BE_STOP', 'STOP'):
                return 'TP1_BE_STOP'
        return base_reason

    def _trigger_tp1(ts):
        nonlocal stop
        if not tp1_enabled or active.get('tp1_hit') or tp1_price is None:
            return
        be_price = compute_net_be_price(
            entry, side, tick,
            fee_rate=float(active.get('fee_rate', 0.)),
            slip_rate=float(active.get('slippage_rate', 0.)),
        )
        moved = False
        if side * (be_price - stop) > tick / 2:
            stop = be_price
            moved = True
        active.update(
            tp1_hit=True,
            tp1_hit_ts=ts,
            tp1_fill_price=tp1_price,
            partial_pct=partial_pct,
            remaining_pct=round(1.0 - partial_pct, 2),
            be_armed=True,
            stop_effective_ts=ts,
            trail_reason='TP1_50_BE',
            time_stop_countdown_min=None,
            action_alert={
                'code': 'TP1_50_BE',
                'level': 'success',
                'ts': ts,
                'title_ua': f'⚡ УВАГА: ФІКСУЄМО {int(round(partial_pct * 100))}% (TP1 +{tp1_r:.2f} R) · СТОП ПЕРЕНЕСЕНО В Б/У',
                'detail_ua': (
                    f'Фіксація 50% по {tp1_price}. '
                    f'{"Стоп залишку перенесено в Б/У (" + str(stop) + "). " if moved else ""}'
                    f'Решта 50% іде як Runner до TP {tp}.'
                ),
            },
        )

    def finish(reason, price, ts):
        active.update(stop=stop, peak_r=peak, processed_until_ts=cursor, quality=quality)
        return {'reason': _exit_reason(reason), 'exit': price, 'closed_ts': ts, 'quality': quality}

    def fill(ts):
        nonlocal pending, start, quality
        pending = False
        start = ts
        quality = 'AMBIGUOUS_LIMIT_FILL' if quality == 'OBSERVED_M1' else quality
        active.update(phase='OPEN', opened_ts=ts, opened_kyiv=None, peak_r=0., be_armed=False)

    def cross(lo, hi, o, ts):
        # Stop-first if both levels touched; adverse gaps execute at open, not the old stop.
        stop_hit = lo <= stop if side == 1 else hi >= stop
        tp_hit = hi >= tp if side == 1 else lo <= tp
        if stop_hit:
            if tp_hit and quality == 'OBSERVED_M1':
                active['ambiguous_ohlc'] = True
            return ('BE_STOP' if active.get('be_armed') else 'STOP', min(o, stop) if side == 1 else max(o, stop))
        if tp_hit:
            return ('TAKE_PROFIT', tp)
        return None

    for ts, c in sorted(bars.items()):
        end = ts + step
        if end <= cursor or end > now:
            continue
        if ts > cursor:
            quality = 'DATA_GAP'
            active['data_gap'] = True
        partial = ts < cursor or ts < start
        try:
            o, h, l, cl = map(float, (c['o'], c['h'], c['l'], c['c']))
        except (KeyError, ValueError, TypeError):
            quality = 'DATA_GAP'
            continue
        if not all(math.isfinite(v) for v in (o, h, l, cl)) or not 0 < l <= min(o, cl) <= max(o, cl) <= h:
            quality = 'DATA_GAP'
            continue
        if partial:
            if quality == 'OBSERVED_M1':
                could_touch = (l <= stop or h >= tp or h >= entry + .9 * risk) if side == 1 else (h >= stop or l <= tp or l <= entry - .9 * risk)
                if pending:
                    could_touch = True
                if could_touch:
                    quality = 'PARTIAL_ENTRY_BAR'
            o = h = l = cl
            active['partial_entry_bar'] = True
        if pending:
            if ts >= expires or end > expires:
                return finish('LIMIT_EXPIRED', entry, expires)
            touched = l <= entry if side == 1 else h >= entry
            if touched and end <= expires:
                fill(end)
                if (l <= stop if side == 1 else h >= stop):
                    cursor = end
                    return finish('STOP', min(o, stop) if side == 1 else max(o, stop), end)
                cursor = end
                continue
            if (h >= tp if side == 1 else l <= tp):
                return finish('LIMIT_MISSED_TARGET', entry, end)
            cursor = end
            continue

        hit = cross(l, h, o, end)
        cursor = end
        if hit:
            # If full TP hit directly on a big impulse bar before TP1 was marked, record full TP honestly.
            return finish(hit[0], hit[1], end)
        if end >= start + 18 * 3600000:
            return finish('TIME_EXIT', cl, end)

        peak = max(peak, (h - entry) / risk if side == 1 else (entry - l) / risk)

        # Two-stage TP1 (50% partial lock + auto-BE) checked after bar close (causal: BE applies to later bars).
        if tp1_enabled and not active.get('tp1_hit') and tp1_price is not None:
            tp1_touched = (h >= tp1_price) if side == 1 else (l <= tp1_price)
            if tp1_touched:
                _trigger_tp1(end)

        # Confirmed structural M5 trailing (post-bar update).
        if active.get('geometry_policy') == POLICY:
            if not partial:
                prev_stop = stop
                stop = structural_trail(active, (o, h, l, cl), stop, peak, end, tick, step)
                if side * (stop - prev_stop) > tick / 2:
                    active['action_alert'] = {
                        'code': 'STRUCTURAL_TRAIL',
                        'level': 'success',
                        'ts': end,
                        'title_ua': f'🛡 УВАГА: СТОП ПЕРЕНЕСЕНО ЗА M5-СТРУКТУРОЮ ({stop})',
                        'detail_ua': f'Підтверджено структурний M5-свінг. Новий стоп: {stop}.',
                    }
        else:
            lock = .55 if peak >= 1.40 else .05 if peak >= .90 else None
            if lock is not None:
                new = round_price(entry + side * lock * risk, tick)
                if side * (new - stop) > 0:
                    stop = new
                    active.update(be_armed=True, stop_effective_ts=end)

        # Alpha-Decay Time-Stop (only when trade has NOT hit TP1 and has no momentum after 45–60 min).
        if time_stop_enabled and not active.get('tp1_hit'):
            elapsed_min = (end - start) / 60000.0
            cur_r = side * (cl - entry) / risk if risk > 0 else 0.0
            if elapsed_min >= stale_exit_min and peak < 0.55 and -0.25 <= cur_r <= 0.45:
                return finish('STALE_EXIT', cl, end)
            if elapsed_min >= stale_tighten_min and peak < 0.55 and -0.35 <= cur_r <= 0.45:
                prev_stop = stop
                stop = stale_tighten_stop(active, stop, cl, end, tick)
                rem_min = max(1, int(round(stale_exit_min - elapsed_min)))
                active['time_stop_countdown_min'] = rem_min
                if side * (stop - prev_stop) > tick / 2 or not active.get('action_alert'):
                    active['action_alert'] = {
                        'code': 'STALE_TIGHTEN',
                        'level': 'warn',
                        'ts': end,
                        'title_ua': f'⏱ УВАГА: СТОП ПЕРЕНЕСЕНО (ТАЙМ-СТОП) · ЗАКРИТТЯ ЧЕРЕЗ ~{rem_min} ХВ',
                        'detail_ua': f'Немає імпульсу {int(elapsed_min)} хв. Стоп підтягнуто до {stop}. Якщо ціна не піде, угоду буде закрито достроково через ~{rem_min} хв.',
                    }

    if now - cursor > step * 2:
        quality = 'DATA_GAP'
        active['data_gap'] = True

    if pending:
        if now >= expires:
            return finish('LIMIT_EXPIRED', entry, expires)
        if quote_ok and quote_ts >= cursor and quote_ts > start:
            fill_quote = float(item.get('ask' if side == 1 else 'bid') or quote)
            if side * (fill_quote - entry) <= 0:
                fill(quote_ts)
                cursor = max(cursor, quote_ts)
                if side * (quote - stop) <= 0:
                    return finish('STOP', quote, quote_ts)
    elif quote_ok and quote_ts >= cursor and quote_ts > start:
        if side * (quote - stop) <= 0:
            if quote_ts > cursor and quality == 'OBSERVED_M1':
                quality = 'QUOTE_EXIT_UNOBSERVED_INTRABAR'
            return finish('BE_STOP' if active.get('be_armed') else 'STOP', quote, quote_ts)
        if side * (quote - tp) >= 0:
            if quote_ts > cursor and quality == 'OBSERVED_M1':
                quality = 'QUOTE_EXIT_UNOBSERVED_INTRABAR'
            return finish('TAKE_PROFIT', tp, quote_ts)
        if tp1_enabled and not active.get('tp1_hit') and tp1_price is not None:
            if side * (quote - tp1_price) >= 0:
                _trigger_tp1(quote_ts)
        if now - start >= 18 * 3600000:
            return finish('TIME_EXIT', quote, quote_ts)
        if time_stop_enabled and not active.get('tp1_hit'):
            elapsed_min = (now - start) / 60000.0
            cur_r = side * (quote - entry) / risk if risk > 0 else 0.0
            if elapsed_min >= stale_exit_min and peak < 0.55 and -0.25 <= cur_r <= 0.45:
                return finish('STALE_EXIT', quote, quote_ts)
            if 30.0 <= elapsed_min < stale_exit_min and peak < 0.55 and cur_r <= 0.45:
                rem_min = max(1, int(round(stale_exit_min - elapsed_min)))
                active['time_stop_countdown_min'] = rem_min
                if elapsed_min >= stale_tighten_min and not active.get('action_alert'):
                    active['action_alert'] = {
                        'code': 'STALE_COUNTDOWN',
                        'level': 'warn',
                        'ts': quote_ts,
                        'title_ua': f'⏱ УВАГА: УГОДА БУДЕ ЗАКРИТА ЧЕРЕЗ ~{rem_min} ХВ (НЕМАЄ ІМПУЛЬСУ)',
                        'detail_ua': f'Позиція без розвитку {int(elapsed_min)} хв (Alpha-Decay Time-Stop). Готуйтесь до дострокового закриття.',
                    }

    active.update(
        stop=stop, peak_r=peak, processed_until_ts=cursor, quality=quality,
        current_price=quote if quote_ok else active.get('current_price', entry),
    )
    return None
