"""Short Ukrainian lifecycle reports. Formatting never changes trading state."""
from __future__ import annotations

import re
from datetime import datetime
from math import isfinite
from zoneinfo import ZoneInfo

import position_monitor
import setup_scanner as scanner


SETUP_LABELS = {
    'SWEEP_RECLAIM': 'Повернення після зняття мінімуму',
    'CAPITULATION_RECOVERY': 'Відновлення після капітуляції',
    'RANGE_EDGE_REVERSAL': 'Розворот від межі діапазону',
    'FAILED_AUCTION_REJECTION': 'Відмова від рівня',
    'LIQUIDITY_SWEEP_REVERSAL_SHORT': 'Розворот після зняття максимуму',
    'BUYER_EXHAUSTION_SHORT': 'Виснаження покупців',
    'PULLBACK_CONTINUATION': 'Відкат за трендом',
    'FRESH_BASE_CONTINUATION': 'Відкат до свіжої бази',
    'ACCEPTANCE_RETEST_CONTINUATION': 'Ретест прийнятого пробою',
    'MOMENTUM_NO_PULLBACK_CONTINUATION': 'Ретест після сильного імпульсу',
    'ACCELERATION_PULLBACK_REENTRY': 'Вхід після відкату другої хвилі',
    'DIRECTION_FLIP_15M': 'Ретест зміни напрямку',
    'TREND_IGNITION': 'Ретест виходу зі стиснення',
    'BREAKOUT_RETEST': 'Ретест пробитого рівня',
    'BOS_IMPULSE_BREAKOUT': 'Імпульсний пробій структури',
    'EMA9_MOMENTUM_SURF': 'Продовження від EMA9',
    'V_REVERSAL_CHOCH': 'Розворот із пробоєм шиї',
    'FAILED_BREAKOUT_TRAP': 'Хибний пробій із поверненням',
    'RANGE_COMPRESSION_BREAKOUT': 'Межа стиснутого діапазону',
    'OPENING_RANGE_BREAKOUT': 'Пробій початкового діапазону',
    'LIQUIDITY_LADDER': 'Рівень повторних дотиків',
    'FAILED_OPENING_RANGE_BREAKOUT': 'Хибний пробій початкового діапазону',
    'FAILED_BREAKOUT_SHORT': 'Хибний пробій максимуму',
    'MSS_REVERSAL_SHORT': 'Ретест розвороту вниз',
    'OR_FAILURE_2_SHORT': 'Повторна відмова від максимуму діапазону',
    'SESSION_MEAN_RECLAIM': 'Повернення до VWAP',
    'DAILY_WEEKLY_OPEN_RECLAIM': 'Повернення до відкриття дня / тижня',
    'TIME_OF_DAY_ADAPTIVE': 'Ретест межі азійського діапазону',
}

HEALTH_LABELS = {
    'SPREAD_TOO_WIDE': 'широкий спред',
    'UNTRUSTED_PRICE': 'немає підтвердженої ціни',
    'STALE_TICKER': 'ціна застаріла',
    'WRONG_INSTRUMENT': 'дані іншого інструмента',
    'MISSING_OR_INVALID_SPREAD': 'немає коректних котирувань',
    'MISSING_LIFECYCLE_BARS': 'бракує свічок для перевірки виконання',
    'LIFECYCLE_HISTORY_GAP': 'бракує свічок для перевірки виконання',
}


def health_text(code):
    if code in HEALTH_LABELS:
        return HEALTH_LABELS[code]
    for prefix, label in [('INSUFFICIENT_', 'замало свічок'),
                          ('STALE_', 'застарілі свічки'), ('GAP_', 'пропуск свічок')]:
        if code.startswith(prefix):
            return label + ' ' + code[len(prefix):].lower()
    return 'неповні дані для перевірки виконання'


def setup_text(plan):
    source = plan.get('source_strategy')
    if source == scanner.wave.BASE:
        return 'Наторговка після імпульсу'
    if source == 'BREAKER_FLIP_15M':
        return 'Breaker • ретест пробою'
    if source == 'MOMENTUM_STAIRCASE_15M':
        return 'Сходинки тренду'
    name = plan.get('setup', plan.get('setup_type', ''))
    return SETUP_LABELS.get(name, name or 'Сетап')


def targets(plan):
    from bzu_policy import SCALE_POLICY, partial_target

    if plan.get('scale_policy') == SCALE_POLICY:
        return f"TP1: {partial_target(plan):.8g} • 50% на +1R | TP2: {plan['tp2']:.8g}"
    parts=[]
    for name in ('TP1','TP2','TP3'):
        level=plan.get(name.lower(),plan.get('tp') if name=='TP1' else None)
        parts.append(f'{name}: {level:.8g}' if level is not None else f'{name}: —')
    return ' • '.join(parts)


def full_exit_text(plan):
    if plan.get('scale_policy') == 'SCALE_OUT_1R':
        return ('Залишок до TP2 • стоп за свінгами М15' if plan.get('tp1_hit') else
                'Після TP1 стоп на +0.15R, далі за свінгами М15')
    name=plan.get('full_exit_target')
    if not name:
        names=[n for n in ('TP1','TP2','TP3') if not plan.get(n.lower()+'_hit')
               and plan.get('partials',{'TP1':1.}).get(n,0)>0]
        name=names[0] if names else 'TP1'
    if plan.get('tp1_decision_gate') and not plan.get('tp1_hit'):
        return 'Рішення про вихід — після TP1'
    if plan.get('awaiting_tp1_review'):
        return 'TP1 досягнуто — перевіряємо продовження'
    return 'Повний вихід: '+name


def close_reason(trade):
    hits = [name for name in ('TP1', 'TP2', 'TP3') if trade.get(name.lower() + '_hit')]
    reason = {'LIQUIDITY_REJECTION_EXIT': 'захист після зняття ліквідності',
              'MFE_PROTECTIVE_EXIT': 'захист після сприятливого руху',
              'STOP': 'стоп', 'STOP_GAP': 'стоп із розривом ціни',
              'TAKE_PROFIT': 'тейк-профіт',
              'CLOSED_ROUND_TRIP_BE': 'повернення до входу після піку',
              'CLOSED_REVERSAL_FLIP_PROFIT': 'зафіксовано прибуток перед переворотом',
              'CLOSED_BOUNCE_BREAKEVEN': 'спрацював стоп на вході після поглинання',
              'CLOSED_STALE_TIME_STOP': 'шестигодинний тайм-стоп',
              'CLOSED_ADVERSE_STRUCTURE_SHIFT': 'зміна структури або режиму проти позиції',
              'CLOSED_EARLY_IMPULSE_FAILURE': 'ранній зустрічний імпульс після MARKET-входу',
              'CLOSED_REPLACED_BY_FRESH_SETUP': 'заміна свіжим сетапом A або A+',
              'CLOSED_STALE_TIMEOUT': 'тайм-стоп на ретесті входу',
              'CLOSED_ADVERSE_CHOCH': 'ретест після зустрічного CHoCH',
              'CLOSED_STALE_REPLACEMENT': 'заміна на підтверджений A+', 'ADAPTIVE_FULL_EXIT':'повний вихід за зміною умов', 'INTRADAY_TIME_EXIT': 'за часом'}.get(
                  trade.get('close_reason', trade.get('reason')), 'завершення')
    detail = (', '.join(hits) + ' досягнуто; ' if hits else '') if trade.get('exit_policy') != 'SINGLE_FULL_EXIT' else ''
    if trade.get('ambiguous_ohlc'):
        reason += '; порядок SL/TP невідомий, враховано стоп'
    return detail + reason


def duration_text(ms):
    mins = max(0, ms // 60_000)
    return f'{mins // 60} год {mins % 60} хв'


def position_diagnostics(position, now, cfg, engine, *, frozen=False):
    import oil_risk

    view = position_monitor.guard(position,now,structural_only=oil_risk.enabled(cfg))
    mode = view['mode']
    time = duration_text(view['time_in_trade_ms'])
    lines = [f"⏱ {time} • MFE {view['max_favorable_r']:+.2f}R ({view['peak_price']:.8g}) • MAE {(-view['max_adverse_r']):.2f}R"]
    amendment = engine.bzu_policy.protective_amendment(position) or {}
    mfe_armed = amendment.get('policy') == position_monitor.MFE_STOP_POLICY
    mfe_active = position_monitor.is_mfe_stop(position)
    if mfe_armed:
        lines.append(f"Захист MFE • SL {amendment['price']:.8g} з наступної свічки.")
    elif mfe_active:
        lines.append(f"Захист MFE • діє SL {position['stop']:.8g}.")
    if mode == 'POST_TP1':
        if position.get('scale_policy') == 'SCALE_OUT_1R':
            effective = position.get('pending_stop') or {}
            stop = effective.get('price', position['stop'])
            timing = ' з наступної свічки' if effective else ''
            return [*lines, f'50% зафіксовано на +1R. SL залишку {stop:.8g}{timing}.']
        if mfe_armed or mfe_active:
            return [*lines, 'TP1 зафіксовано. Залишок ведемо до TP2.']
        return [*lines, 'TP1 зафіксовано. Залишок ведемо за штатним стопом до TP2.']
    if view['structural_only']:
        if mode == 'STRUCTURAL_HOLD':
            if not (mfe_armed or mfe_active):
                until = engine.report_deadline(view['structural_hold_until'],now,cfg)
                lines.append(f'Початковий структурний SL до {until}.')
        else:
            lines.append('Структурний вихід після двох закриттів M15 за свінгом, EMA20 та VWAP.')
        return lines
    deadline = view['stale_at']
    moment = engine.report_deadline(deadline, now, cfg)
    if now < deadline:
        lines.append(f'⏳ Контроль імпульсу до {moment} (залишилось {duration_text(deadline-now)}).')
    else:
        if view['stale_stop'] is not None:
            outcome = f"ризик скорочено, SL {position['stop']:.8g}"
        elif view['max_favorable_r'] >= view['stale_mfe_r'] - 1e-8:
            outcome = f"пік досяг порога +{view['stale_mfe_r']:.2f}R"
        elif frozen:
            outcome = 'чекаємо свіжих даних для застосування'
        else:
            outcome = 'діє перевірка рівня −0.35R'
        lines.append(f'🚨 Тайм-стоп {moment} вичерпано (прострочено на {duration_text(now-deadline)}) · {outcome}.')
    if mode == 'ADVERSE_CHOCH' or view['structure'] in ('ADVERSE', 'COUNTER_TREND'):
        lines.append('Структура або режим проти позиції.')
    if view['max_favorable_r'] >= view['round_trip_mfe_r'] - 1e-8 and now >= view['round_trip_at']:
        lines.append(f"Round-Trip активний від входу {position['entry']:.8g}. Вихід при +0.10R або нижче.")
    elif now < view['round_trip_at'] or mode == 'OBSERVING':
        lines.append('Round-Trip перевіряє пік від +0.40R після 90 хвилин.')
    return lines


def candidate_reason(row, cfg, engine):
    reason = row.get('reason')
    if reason == 'SETUP_CONDITIONS_INCOMPLETE':
        missing = [c['condition'] for c in row.get('setup_conditions', row.get('conditions', []))
                   if c.get('met') is not True] or row.get('missing_conditions', [])
        return 'Чекаємо: ' + (missing[0] if missing else 'підтвердження патерну')
    if reason in ('TARGET_RR_TOO_LOW', 'NEAREST_TARGET_TOO_CLOSE_AFTER_COSTS') and row.get('gross_rr') is not None:
        return f"Вхід заблоковано: TP дає {row['gross_rr']:.2f}R; мінімум {cfg.min_net_rr:.2f}R"
    if reason in ('ACCEPTED', 'CONFIRMED'):
        return 'Ордер ще не активований: перевіряємо доступність входу.'
    return 'Вхід заблоковано: ' + engine.REASON_UA.get(reason, 'умови входу ще не виконані')


def focus_lines(focus):
    primary = focus['primary']
    if not primary:
        return ['', focus['headline'] + ' • немає допущеного входу.']
    side = 'LONG' if primary['side']==1 else 'SHORT'
    if primary['status'] != 'DEVELOPING':
        return ['', f"{focus['headline']} • {side} @ {primary['level']:.2f}"]
    preview = primary['preview']
    title = 'СПОСТЕРЕЖЕННЯ' if preview.get('gate') else 'АКТИВНИЙ ПОШУК'
    line = f"{title} {side} • Робоча зона {primary['level']:.2f}"
    if preview.get('stop') is not None and preview.get('tp1') is not None:
        line += f" (Стоп {preview['stop']:.2f} | Тейк {preview['tp1']:.2f})"
    return ['', line]


_DIVIDER = '━━━━━━━━━━━━━━━━━━'
_DIRECTION = {1: '🟢 LONG', -1: '🔴 SHORT'}


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if isfinite(number) else None


def _price(value):
    return f'{value:.2f} $' if value is not None else '—'


def _pnl(net_r, risk=None):
    if net_r is None:
        return 'недоступний'
    units = f' ({net_r * risk:+.2f} $/од.)' if risk is not None and risk > 0 else ''
    return f'{net_r:+.2f}R{units}'


def _estimated_net_r(plan, price, cfg, engine):
    """Estimate the entire filled position including executed partial exits."""
    risk = _number(plan.get('risk_budget', plan.get('risk')))
    entry = _number(plan.get('entry'))
    side = plan.get('side')
    if risk is None or risk <= 0 or entry is None or side not in _DIRECTION:
        return None
    cost = engine.execution_config(plan, cfg)
    remaining = plan.get('remaining', 1.)
    gross = plan.get('realized_gross_r', 0.) + remaining * side * (price - entry) / risk
    entry_cost = plan.get('entry_cost', entry)
    fees = 0.
    if cost.include_commission:
        fees = ((entry_cost * cost.maker_fee + remaining * price * cost.taker_fee) / risk
                + plan.get('realized_exit_fees_r', 0.))
    slip = remaining * price * cost.slippage_bps / 10_000 / risk
    return _number(gross - fees - slip)


def _daily_trades(state, now, tz):
    today = datetime.fromtimestamp(now / 1000, tz).date()
    daily = []
    for trade in state.get('trades', []):
        ts = _number(trade.get('closed_ts'))
        if ts is not None and 0 < ts <= now and _number(trade.get('net_r')) is not None:
            if datetime.fromtimestamp(ts / 1000, tz).date() == today:
                daily.append(trade)
    return daily


def _daily_noun(count):
    if count % 10 == 1 and count % 100 != 11:
        return 'закрита угода'
    if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14):
        return 'закриті угоди'
    return 'закритих угод'


def _reported_closes(state, result, now):
    events = {
        event['order_id']: event for event in result.get('events', [])
        if event.get('kind') == 'CLOSED' and event.get('order_id') and 0 < event['ts'] <= now
    }
    if not events:
        return []
    trades = {trade.get('id'): trade for trade in state.get('trades', [])
              if trade.get('id') in events}
    closes = []
    for order_id, event in sorted(events.items(), key=lambda item: item[1]['ts'], reverse=True):
        trade = trades.get(order_id, {})
        closes.append({**trade, 'id': order_id, 'net_r': event.get('net_r', trade.get('net_r')),
                       'close_reason': event.get('reason') or trade.get('close_reason'),
                       'ambiguous_ohlc': event.get('ambiguous_ohlc', trade.get('ambiguous_ohlc', False))})
    return closes


def _report_setup(row):
    zone = row.get('entry_zone') or row.get('zone') or {}
    timeframe = zone.get('timeframe') or row.get('timeframe') or scanner.timeframe(row)
    key = row.get('source_strategy') or row.get('setup') or row.get('kind') or zone.get('kind')
    name = setup_text({**row, 'setup': key})
    if name == key and key:
        name = {
            'DEMAND_OB': 'OB попиту', 'SUPPLY_OB': 'OB пропозиції',
            'FVG_BULL': 'Бичачий FVG', 'FVG_BEAR': 'Ведмежий FVG',
        }.get(key, row.get('label') if row.get('label') != key else None) or 'Структурний сетап'
    return f'{timeframe} {name}'


def _waiting_lines(state, snapshot, result, cfg, engine, fresh):
    projection = result.get('public_projection') or {}
    analysis = result.get('analysis') or {}
    focus = projection.get('scenario_plan') or analysis.get('scenario_plan') or {}
    all_waiting = engine.pending_plans(state)
    pending = [plan for plan in all_waiting if not plan.get('cancel_requested_ts')]
    candidate = None
    if fresh:
        primary_id = projection.get('primary_candidate_id')
        projected = next((row for row in projection.get('candidates', [])
                          if primary_id and row.get('id') == primary_id), None)
        candidate = (projected or focus.get('primary') or analysis.get('best_scenario')
                     or scanner.best_scenario(analysis.get('watch', []), snapshot.price))
        if candidate and candidate.get('status') in ('CANCELLING', 'FOLLOWED', 'PENDING', 'POSITION', 'ERROR'):
            candidate = None
    focus_market = focus.get('market') or {}
    regime = projection.get('market_regime')
    if fresh and not (regime or focus_market):
        regime = engine.policy.execution_regime(snapshot, cfg, engine).get('state')
    trend = (projection.get('primary_bias') or focus_market.get('trend_side')
             or engine.policy.regime_side({'state': regime})) if fresh else 0
    side = (pending[0]['side'] if pending else (candidate or {}).get('side')) or trend
    regime_text = {1: 'висхідний', -1: 'низхідний'}.get(trend)
    if fresh and (regime in ('RANGE', 'RANGE_LIQUIDITY_ROTATION') or focus_market.get('phase') == 'RANGE'):
        regime_text = 'діапазон'
    priority = _DIRECTION.get(side, '⚪️ НЕЙТРАЛЬНО')
    if regime_text:
        priority += f' ({regime_text})'
    lines = [f'🧭 Пріоритет: {priority}']
    if pending:
        for plan in pending:
            lines.append(f"🎯 Очікуємо: {_report_setup(plan)} · ліміт {_DIRECTION.get(plan['side'], '—')}"
                         f" @ {_price(_number(plan['entry']))} (виставлено)")
    elif any(view.get('reconciliation') for _, view in engine.slot_views(state)):
        lines.append('🎯 Очікуємо: підтвердження виконання за журналом')
    elif all_waiting:
        lines.append('🎯 Очікуємо: підтвердження скасування ліміту')
    elif candidate:
        zone = candidate.get('entry_zone') or candidate.get('zone') or candidate
        low, high = _number(zone.get('low')), _number(zone.get('high'))
        level = _number(candidate.get('level') or candidate.get('entry'))
        label = _report_setup(candidate)
        if low is not None and high is not None and 0 < low < high:
            lines.append(f'🎯 Очікуємо: тест зони {low:.2f}–{high:.2f} $ ({label})')
        elif level is not None:
            lines.append(f'🎯 Очікуємо: {label} @ {_price(level)} (спостереження)')
        else:
            lines.append(f'🎯 Очікуємо: {label} (спостереження)')
    elif fresh:
        lines.append('🎯 Очікуємо: нову структурну зону (15m/5m)')
    else:
        lines.append('🎯 Очікуємо: оновлення ринкових даних')
    return lines


def build_status_message(state, snapshot, result, cfg, engine):
    """Compose the scheduled Telegram report from the execution ledger."""
    now = max(snapshot.now, state.get('last_run_ts') or 0)
    tz = ZoneInfo('Europe/Kyiv')
    local = datetime.fromtimestamp(now / 1000, tz)
    price = _number(snapshot.price)
    if (not snapshot.trusted or snapshot.instrument != cfg.instrument or price is None
            or price <= 0 or not 0 <= now - snapshot.ticker_ts <= 120_000):
        price = None
    fresh = (price is not None and result.get('action') not in ('DUPLICATE_OR_OUT_OF_ORDER', 'DATA_HOLD')
             and not (result.get('analysis') or {}).get('health'))
    daily = _daily_trades(state, now, tz)
    closes = _reported_closes(state, result, now)
    positions = engine.active_plans(state)

    body = [f'💵 Ціна зараз: {_price(price)}']
    if positions:
        marked = [(plan, _estimated_net_r(plan, price, cfg, engine) if fresh else None)
                  for plan in positions]
        first_pnl = marked[0][1]
        icon = '⚪️' if first_pnl is None else '🟢🚀' if first_pnl >= 0 else '🔴⚠️'
        heading = f'{icon} В УГОДІ'
        for plan, net_r in marked:
            body.append(f"📌 Позиція: {_DIRECTION.get(plan.get('side'), '—')} @ {_price(_number(plan.get('entry')))}")
            arrow = '📊' if net_r is None else '📈' if net_r >= 0 else '📉'
            body.append(f"{arrow} Поточний PnL: {_pnl(net_r, _number(plan.get('risk')))}")
            tp1 = engine.bzu_policy.partial_target(plan)
            body.append(f"🛡 SL: {_price(_number(plan.get('stop')))} | 🎯 TP1: {_price(_number(tp1))}")
    elif closes:
        latest = _number(closes[0].get('net_r'))
        icon = '🏁' if latest is None else '🏁✅' if latest >= 0 else '🏁❌'
        heading = f'{icon} УГОДУ ЗАКРИТО'
    else:
        heading = '⚪️⏳ ОЧІКУЄМО СЕТАП'
        body.extend(_waiting_lines(state, snapshot, result, cfg, engine, fresh))
        if daily:
            previous = max(daily, key=lambda trade: _number(trade['closed_ts']))
            body.append(f"🏁 Остання закрита: {_pnl(_number(previous['net_r']), _number(previous.get('risk')))}")

    for trade in closes:
        side = _DIRECTION.get(trade.get('side'), '—')
        body.append(f"📌 Закрито: {side} {engine.display_order(state, trade['id'])} ({close_reason(trade)})")
        body.append(f"💰 Закритий PnL: {_pnl(_number(trade.get('net_r')), _number(trade.get('risk')))}")

    if positions and not fresh:
        body.append('⚠️ Стан за журналом · ринкові дані не оновлено')
    total_r = sum(_number(trade['net_r']) for trade in daily)
    count = len(daily)
    instrument = 'Brent (BZU)' if cfg.instrument == 'BZ-USDT-SWAP' else cfg.instrument
    return '\n'.join([
        f'{heading} | {instrument}', _DIVIDER, *body, _DIVIDER,
        f'📊 За сьогодні: {total_r:+.2f}R ({count} {_daily_noun(count)})',
        f'🕒 {local:%H:%M} Київ · звіти кожні 15 хв',
    ])


build_15m_status_report = build_status_message


def build(state, snapshot, result, cfg, engine):
    if engine.VERSION in ('full-ict-v12.21.0-bzu-checklist','full-ict-v12.22.0-precision-protection'):
        return build_checklist(state,snapshot,result,cfg,engine)
    now = snapshot.now
    local = datetime.fromtimestamp(now/1000, ZoneInfo(cfg.timezone))
    price_ok = snapshot.trusted and snapshot.price > 0 and 0 <= now-snapshot.ticker_ts <= 120_000
    health = result.get('analysis', {}).get('health', [])
    frozen = (result.get('action')=='DUPLICATE_OR_OUT_OF_ORDER' or
              (result.get('action')=='DATA_HOLD' and state.get('last_run_ts',0)<now))
    price = f'{snapshot.price:.8g}' if price_ok else 'недоступна'
    lines = [f"{cfg.instrument.removesuffix('-SWAP')} • {local:%d.%m %H:%M} (Київ) • {engine.REPORT_RELEASE}",
             f'💰 Ціна зараз: {price}']
    active, pending, uncertain = [], [], []
    for _, view in engine.slot_views(state):
        if view.get('active'):
            active.append(view['active'])
        if view.get('reconciliation'):
            uncertain.append(view['reconciliation'])
    all_waiting=engine.pending_plans(state)
    cancelling=[p for p in all_waiting if p.get('cancel_requested_ts')]
    pending=[p for p in all_waiting if not p.get('cancel_requested_ts')]
    active.sort(key=lambda p: p.get('order_number', 0))
    pending.sort(key=lambda p: p.get('order_number', 0))
    lines += [f'Відкриті угоди: {len(active)} • Ліміти в очікуванні: {len(pending)}']

    if result.get('analysis', {}).get('edge_mode') == 'RESEARCH_ONLY' and not frozen:
        lines += ['Режим дослідження: нові ліміти вимкнено до перевірки переваги; чинні угоди супроводжуються.']

    # Describe a real portfolio rotation rather than the misleading message
    # 'no entries'. A prepared candidate is NOT an actual pending limit.
    rotations=[ev for ev in result.get('events', [])
               if ev.get('kind')=='PORTFOLIO_ROTATION_COMMITTED']
    if rotations and not frozen:
        latest=rotations[-1]
        old_label=engine.display_order(state,latest.get('replaced_id',''))
        new_label=engine.display_order(state,latest.get('replacement_id',''))
        label = 'Вхід MARKET після зняття ліміту' if latest.get('order_type') == 'MARKET' else 'Заміна ліміту'
        lines += [f'{label}: {old_label} → {new_label} • без третього ордера.']
    elif state.get('portfolio_rotation_intent') and not frozen:
        intent=state['portfolio_rotation_intent']
        if intent.get('status')=='PREPARED_WAIT_RECONCILIATION':
            lines += ['Сильніший сценарій підготовлений; перевіряємо виконання старого ліміту перед заміною.']

    diagnostic = result.get('analysis', {}).get('meta_model', {})
    if diagnostic.get('validated'):
        lines += [f'Метафільтр: перевірений на PAPER-епізодах • {diagnostic.get("independent_episodes",0)} незалежних']
    # Do not print an unsupported 70-90% win probability from a cold model.
    if result.get('action')=='DAILY_PAUSE':
        lines += ['Нові входи: досягнуто денний ліміт ризику.']
    if frozen:
        checkpoint = state.get('last_run_ts', 0)
        when = engine.report_deadline(checkpoint, now, cfg) if checkpoint else 'попередній цикл'
        lines += [f'Дані не оновлені. Збережений стан на {when}.']
    focus = None
    if not frozen and not health and result.get('action') != 'DATA_HOLD' and price_ok and engine.oil_risk.enabled(cfg):
        analysis = result.get('analysis', {})
        focus = result.get('public_projection',{}).get('scenario_plan') or analysis.get('scenario_plan')
        if not focus:
            regime = engine.policy.execution_regime(snapshot, cfg, engine)
            focus = engine.scenario_planner.focus(snapshot, cfg, engine, regime,
                orders=engine.open_plans(state), admitted=analysis.get('admitted_plans', []),
                action=result.get('action'))
        lines += focus_lines(focus)
        plans = result.get('public_projection', {}).get('entry_scenarios', {})
        pullback, breakout = plans.get('pullback'), plans.get('breakout')
        if pullback:
            side = 'LONG' if pullback['side'] == 1 else 'SHORT'
            tag = '' if pullback['actionable'] else ' • спостереження'
            lines += [f"План А · {side} відкат {pullback['entry']:.2f} • SL {pullback['stop']:.2f} • "
                      f"TP1 {pullback['tp1']:.2f} • TP2 {pullback['tp2']:.2f}{tag}"]
        if breakout:
            side = 'LONG' if breakout['side'] == 1 else 'SHORT'
            verb = 'вище' if breakout['side'] == 1 else 'нижче'
            label = '' if breakout['base_confirmed'] else ' • структурний орієнтир'
            lines += [f"План Б · {side} після закриття М15 {verb} {breakout['edge']:.2f}{label}"]
    if active:
        lines += ['', f'🔵 СУПРОВІД — {len(active)}']
    for index, p in enumerate(active):
        if index:
            lines += ['']
        label = engine.display_order(state, p['id'])
        side = 'LONG' if p['side'] == 1 else 'SHORT'
        lines += [f"{label} {side} • {scanner.timeframe(p).lower()} • {setup_text(p)}",
                  f"Вхід: {p['entry']:.8g} • SL: {p['stop']:.8g}", targets(p), full_exit_text(p)]
        remaining = p.get('remaining', 1.)
        estimate = 'недоступний'
        if price_ok and not frozen:
            gross = p.get('realized_gross_r', 0) + remaining*p['side']*(snapshot.price-p['entry'])/p['risk']
            cost_cfg=engine.execution_config(p,cfg)
            fees = (p['entry']*p.get('entry_fee_rate', cost_cfg.maker_fee)/p['risk']
                    + p.get('realized_exit_fees_r', 0)
                    + remaining*snapshot.price*cost_cfg.taker_fee/p['risk']) if cost_cfg.include_commission else 0.
            slip = remaining*snapshot.price*cost_cfg.slippage_bps/10_000/p['risk']
            estimate = f'{gross-fees-slip:+.2f}R'
        lines += [f'Результат зараз ≈ {estimate}', *position_diagnostics(p, now, cfg, engine, frozen=frozen)]
        assessment=next((r for r in result.get('analysis',{}).get('position_reviews',[])
                         if r.get('order_id') == p.get('id')),None)
        if assessment and not frozen:
            labels={'INTACT':'🟢 Структура: без підтвердженого розвороту',
                    'WEAKENING':'🟠 Структура: можливе послаблення',
                    'REVERSAL_RISK':'🔴 Структура: ризик розвороту',
                    'INSUFFICIENT':'⚪ Структура: недостатньо свіжих даних'}
            lines += [labels.get(assessment.get('status'),'⚪ Структура: невизначено'),
                      assessment.get('reason','')]
        if p.get('pending_target'):
            amendment=p['pending_target']
            lines += [f"Ціль {amendment['name']} з {engine.report_deadline(amendment['effective_ts'],now,cfg)}"]
        amendment = engine.bzu_policy.protective_amendment(p)
        if amendment:
            lines += [f"Новий SL {amendment['price']:.8g} з {engine.report_deadline(amendment['effective_ts'], now, cfg)}"]

    if pending:
        lines += ['', f'🟡 ОЧІКУЄМО ЛІМІТ — {len(pending)}']
    for p in pending:
        label=engine.display_order(state,p['id'])
        side='LONG' if p['side']==1 else 'SHORT'
        lines += [f"{label} {side} • Вхід: {p['entry']:.8g}"]
        meta_view=p.get('meta_decision') or {}
        if meta_view.get('validated') and meta_view.get('probability') is not None:
            lines += [f"Прогноз TP раніше SL після входу: {meta_view['probability']:.0%} (PAPER-модель, експериментально)"]
    if cancelling:
        lines += ['', 'СКАСУВАННЯ ЛІМІТІВ']
        for p in cancelling:
            reason=engine.REASON_UA.get(p.get('cancel_reason'),'умови входу змінилися')
            lines += [f"{engine.display_order(state,p['id'])} • {reason}; перевіряємо останню свічку"]
    for r in uncertain:
        lines += [f"{engine.display_order(state, r['order_id'])} — виконання НЕВИЗНАЧЕНЕ; потрібна перевірка."]

    events = sorted(result.get('events', []), key=lambda e: e['ts'])
    closed = {e['order_id']: e for e in events if e['kind'] == 'CLOSED'}
    if closed:
        lines += ['', '✅ ЗАКРИТІ УГОДИ']
    for order_id, event in closed.items():
        trade = next((t for t in reversed(state.get('trades', [])) if t['id'] == order_id), {})
        value = event['net_r']
        outcome = 'прибуток' if value > 0 else 'збиток' if value < 0 else 'беззбитково'
        lines += [f"{'✅' if value > 0 else '❌' if value < 0 else '⚪'} {engine.display_order(state, order_id)} ЗАКРИТО: {value:+.2f}R — {outcome} • "
                  f"{engine.report_deadline(event['ts'], now, cfg)}",
                  close_reason({**trade, 'close_reason': event.get('reason', trade.get('close_reason')),
                                'ambiguous_ohlc': event.get('ambiguous_ohlc', False)})]
    no_entry = {e['order_id']: e for e in events if e['kind'] in ('CANCELLED', 'EXPIRED', 'UNCONFIRMED_RELEASED')}
    for order_id, event in no_entry.items():
        if event['kind'] == 'UNCONFIRMED_RELEASED':
            text = 'результат невідомий; виключено з WR'
        else:
            text = 'скасовано без входу' if event['kind'] == 'CANCELLED' else 'строк минув, входу не було'
            reason=event.get('reason')
            if reason=='REPLACED_BY_CLOSER_SETUP':
                text += ' • ближчий підтверджений сетап'
            elif reason in ('DAILY_RISK_LIMIT_REACHED','LEVEL_PASSED_WHILE_UNARMED','EXECUTION_CONTROL_REVERSED',
                            'CAPITULATION_RECOVERY_LOST','PORTFOLIO_DIRECTION_CONFLICT','BETTER_OPPOSING_SCENARIO',
                            'LEGACY_PENDING_EXCESS_CAP_2', 'CANCEL_TARGET_REACHED_WITHOUT_FILL',
                            'DYNAMIC_TREND_TRAILING_REPLACE', 'CANCELLED_REPLACED_BY_LIVE_SETUP', 'LIMIT_TTL'):
                text += ' • '+engine.REASON_UA[reason]
        lines += [f"{engine.display_order(state, order_id)} • {text}"]
    represented = {p['id'] for p in active+pending+cancelling} | set(closed) | set(no_entry)
    represented.update(r['order_id'] for r in uncertain)
    other_events = [engine.message_event(state, event, cfg) for event in events
                    if event.get('order_id') not in represented]
    other_events = [event for event in other_events if event]
    if other_events:
        lines += ['', 'ІНШІ ЗМІНИ'] + other_events

    # A forming setup is not a third pending order. Suppress it when both slots
    # are occupied or market/execution data do not permit a new decision.
    if not focus and not active and not pending and engine.occupied_slots(state) < 2 and not health and not frozen and price_ok:
        analysis = result.get('analysis', {})
        rows = analysis.get('watch', []) or analysis.get('setup_matrix', [])
        live = engine.open_plans(state)
        live_keys = {(p.get('setup'), scanner.timeframe(p)) for p in live}
        rows = [r for r in rows if (r.get('setup'), r.get('timeframe', '15m')) not in live_keys]
        best = scanner.best_scenario(rows, snapshot.price, None)
        if best and best.get('level') and best.get('side') in (-1, 1):
            side = 'LONG' if best['side'] == 1 else 'SHORT'
            ready = best.get('readiness_pct', 0) or 0
            lines += ['', '🟠 СПОСТЕРІГАЄМО — ордер ще не активований',
                      f"{side} • {best.get('timeframe', '15m').lower()} • {setup_text(best)}",
                      (f"Рівень: {best['level']:.8g} • патерн {ready:.0f}%, ліміт не допущено"
                       if best.get('status')=='BLOCKED' and ready >= 100
                       else f"Рівень: {best['level']:.8g} • готовність умов {ready:.0f}%"),
                      candidate_reason(best, cfg, engine)]
    if result.get('action') in ('DAILY_PAUSE', 'SESSION_CLOSED', 'DIRECTION_CONFLICT'):
        lines += ['', engine.REASON_UA.get(result['action'], 'Нові входи на паузі') + '.']
    # Human-facing daily summary only. Keep full lifetime and entry-version
    # cohorts in the state/journal for research; do not relabel legacy trades.
    # Closure day is interpreted in the configured timezone, NOT UTC and NOT
    # the order entry day. Incomplete/unresolved/expired plans are excluded.
    daily = _daily_trades(state, now, local.tzinfo)
    total_r = engine.statistics(daily)['net_r']
    result_r = f'{total_r:+.2f}R' if abs(total_r) > 1e-10 else '0.00R'
    count = len(daily)
    noun = _daily_noun(count)
    lines += ['', f'Сьогодні (Київ): {count} {noun} • результат {result_r}']
    return '\n'.join(lines)


def build_checklist(state, snapshot, result, cfg, engine):
    """Compact v12.22 Telegram report. Presentation only, never mutates the ledger."""
    import directional_policy
    import ict_methodology

    now = snapshot.now
    local = datetime.fromtimestamp(now / 1000, ZoneInfo(cfg.timezone))
    session = ict_methodology.session_liquidity(snapshot)
    regime = directional_policy.execution_regime(snapshot, cfg, engine)
    trend = {
        'BULLISH_TREND': 'BULLISH',
        'BULLISH_ACTIVE': 'BULLISH',
        'BEARISH_TREND': 'BEARISH',
        'BEARISH_ACTIVE': 'BEARISH',
        'RANGE_LIQUIDITY_ROTATION': 'RANGE',
        'PULLBACK_PHASE': 'CONFLICT / PULLBACK',
    }.get(regime.get('state'), 'UNKNOWN')
    price_ok = snapshot.trusted and snapshot.price > 0 and 0 <= now - snapshot.ticker_ts <= 120_000
    price = f'{snapshot.price:.8g}' if price_ok else 'недоступна'
    frozen = result.get('action') in ('DATA_HOLD', 'DUPLICATE_OR_OUT_OF_ORDER')
    lines = [
        f'🛢 BZU • {local:%d.%m %H:%M} (Київ) • {engine.REPORT_RELEASE}',
        f'💰 Ціна: {price} • {session["session"]} • Режим {regime.get("trend_anchor_timeframe") or "ринку"} · {trend}',
    ]

    focus = None
    if not frozen and price_ok and engine.oil_risk.enabled(cfg):
        analysis = result.get('analysis',{})
        focus = analysis.get('scenario_plan') or engine.scenario_planner.focus(
            snapshot,cfg,engine,regime,orders=engine.open_plans(state),
            admitted=analysis.get('admitted_plans',[]),action=result.get('action'))
        lines[1] = f'💰 Ціна: {price} • {session["session"]}'
        lines += focus_lines(focus)
    # Show only genuine new orders, not duplicate scenario/matrix diagnostics.
    selected = [] if frozen else result.get('selected_plans', [])
    if selected:
        lines += ['', '🆕 НОВИЙ СИГНАЛ']
    for p in selected:
        active_entry = p.get('action_type') == 'ACTIVE_ENTRY'
        kind = 'АКТИВНИЙ ВХІД' if active_entry else 'НОВИЙ ЛІМІТ'
        side = 'LONG' if p['side'] == 1 else 'SHORT'
        lines += [
            f'🟢 {kind} {engine.display_order(state, p["id"])} {side} • {p.get("setup_timeframe", "15m")} • {setup_text(p)}',
            f'Вхід: {p["entry"]:.8g} • SL: {p["stop"]:.8g} • TP1: {p["tp1"]:.8g}'
            + (f' • TP2: {p["tp2"]:.8g}' if p.get('tp2') is not None else ''),
        ]

    pending = engine.pending_plans(state)
    active = engine.active_plans(state)
    uncertain = [view['reconciliation'] for _, view in engine.slot_views(state)
                 if view.get('reconciliation')]
    occupied = len(pending) + len(active) + len(uncertain)
    status = f'📌 Місця: {occupied}/2 • 🔵 Супровід: {len(active)} • ⏳ Ліміти: {len(pending)}'
    if frozen:
        # Never present an unverified lifecycle as freshly reconciled. No long stale-data paragraph.
        status += ' • ⚠️ не перевірено'
    lines += ['', status]
    if uncertain:
        lines += [f'⚠️ Невизначене виконання: {len(uncertain)} • місце зарезервовано']

    # Portfolio rotation is only shown after a confirmed replacement.
    # It does not mean that the third limit was placed simultaneously.
    rotation_events = ([] if frozen else [ev for ev in result.get('events', [])
                       if ev.get('kind') == 'PORTFOLIO_ROTATION_COMMITTED'])
    for ev in rotation_events:
        old = engine.display_order(state, ev.get('replaced_id', ''))
        new = engine.display_order(state, ev.get('replacement_id', ''))
        lines += [f'🔁 Заміна ліміту: {old} → {new} • без третього ордера']

    # Events are important even in a short report: always retain fills, exits,
    # cancellations and protective-stop changes.
    records = {p['id']: p for p in state.get('orders', []) + state.get('trades', []) + pending + active}
    for ev in ([] if frozen else result.get('events', [])):
        kind = ev.get('kind')
        p = records.get(ev.get('order_id'), {})
        label = engine.display_order(state, ev.get('order_id', ''))
        when = engine.local_time(ev['ts'], cfg)
        if kind == 'FILLED':
            lines += [f"✅ ОРДЕР АКТИВОВАНО {label} @ {p.get('entry', ev.get('entry', 0)):.8g} • {when}"]
        elif kind == 'SECOND_LIMIT_FILLED':
            lines += [f"✅ Друга частина {label}: середній вхід {ev['entry']:.8g} • {when}"]
        elif kind in ('BREAKEVEN_ARMED', 'STRUCTURAL_TRAIL_ARMED'):
            lines += [f"🛡️ {label}: SL → {ev['stop']:.8g} • з наступної свічки"]
        elif kind == 'PAPER_STOP_AMENDED' and ev.get('reason') in ('TIME_DECAY_TRAIL','MFE_PROTECTION'):
            detail = 'часовий супровід' if ev['reason']=='TIME_DECAY_TRAIL' else 'зменшення ризику після +0.60R'
            lines += [f"🛡️ {label}: SL → {ev['stop']:.8g} • {detail}"]
        elif kind == 'TP1_PARTIAL':
            if ev.get('scale_policy') == 'SCALE_OUT_1R':
                lines += [f"🎯 {label}: 50% зафіксовано на +1.00R @ {ev['price']:.8g}"]
            else:
                lines += [f"🎯 Частину {label} зафіксовано на TP1 • чекаємо підтвердження M5 для Б/У"]
        elif kind in ('CANCELLED', 'EXPIRED', 'CANCEL_REQUESTED'):
            reasons = {'TARGET_REACHED_WITHOUT_FILL': 'TP1 досягнуто без входу',
                       'LIMIT_TTL': 'минув час дії', 'SESSION_ENDED': 'сесія закінчилася',
                       'STRUCTURE_FLIPPED': 'підтверджено злам структури',
                       'LEVEL_PASSED_WHILE_UNARMED': 'рівень пройдено до активації',
                       'DYNAMIC_ORDER_STEP': 'нова ближча структурна зона'}
            reason = reasons.get(ev.get('reason'), engine.REASON_UA.get(ev.get('reason'), 'сценарій скасовано'))
            lines += [f"❌ ЛІМІТ ЗНЯТО {label} @ {p.get('limit_price', p.get('entry', 0)):.8g} • {reason}"]
        elif kind == 'CLOSED':
            net = ev.get('net_r', 0)
            outcome = 'прибуток' if net > 0 else 'збиток' if net < 0 else 'беззбиток'
            lines += [f"{'✅' if net > 0 else '🔴' if net < 0 else '⚪'} {label} ЗАКРИТО: {net:+.2f}R — {outcome} • "
                      f"{close_reason({**p, 'close_reason': ev.get('reason')})}"]

    if active:
        lines += ['', f'🔵 СУПРОВІД — {len(active)}']
    for p in active:
        side = 'LONG' if p['side'] == 1 else 'SHORT'
        lines += [
            f'🟢 {engine.display_order(state, p["id"])} {side} • вхід {p["entry"]:.8g}',
            f'🛡️ SL: {p["stop"]:.8g} • 🎯 TP1: {p.get("tp1", p["tp"]):.8g}'
            + (f' • TP2: {p["tp2"]:.8g}' if p.get('tp2') is not None else ''),
        ]
        lines += position_diagnostics(p, now, cfg, engine, frozen=frozen)
        amendment = engine.bzu_policy.protective_amendment(p)
        if amendment:
            lines += [f"🛡️ Новий SL: {amendment['price']:.8g} • з {engine.local_time(amendment['effective_ts'], cfg)}"]

    if pending:
        lines += ['', f'⏳ ОЧІКУВАННЯ — {len(pending)}']
    for p in pending:
        left = max(0, (p['expires_ts'] - now) // 60_000)
        side = 'LONG' if p['side'] == 1 else 'SHORT'
        distance = ''
        if price_ok and not frozen:
            absolute = abs(snapshot.price - p['entry'])
            distance = f' • ↔ до входу: {absolute:.3f} ({absolute / snapshot.price * 100:.2f}%)'
        lines += [
            f'🟡 {engine.display_order(state, p["id"])} {side} • ліміт {p["entry"]:.8g}',
            f'🛡️ SL: {p["stop"]:.8g}{distance} • ⏱ ще {left} хв',
        ]

    # A research/watch candidate is not a pending order. Do not print long
    # rejection diagnostics while real orders are already occupying the slots.
    if not focus and not pending and not active and not frozen and result.get('analysis', {}).get('entry_mode') in ('LIVE_REACTION','HYBRID'):
        import live_terminal
        public = live_terminal.build(state, snapshot, result, cfg, engine)
        primary = next((p for p in public['candidates'] if p['id'] == public['primary_candidate_id']), None)
        if primary:
            side = 'LONG' if primary['side'] == 1 else 'SHORT'
            lines += ['', f'👁 ПЛАН LIVE РЕАКЦІЇ • {side} @ {primary["entry"]:.8g}',
                      f'SL {primary["stop"]:.8g} • TP1 {primary["tp1"]:.8g} • TP2 {primary["tp2"]:.8g}',
                      'Попередній план не є ордером. Перевірте тактику та допуск у дашборді.']
        else:
            lines += ['', '👁 LIVE REACTION: чекаємо повний якісний план; ордер не активовано.']
    if not focus and not pending and not active and not frozen and not selected and result.get('analysis', {}).get('entry_mode') not in ('LIVE_REACTION','HYBRID'):
        waiting = [p for p in state.get('watch', []) if p.get('reason') in (
            'WAIT_RETEST', 'IMPULSE_CONFIRMATION_REQUIRED', 'LOCAL_STRUCTURE_CONFLICT',
            'SAME_SIDE_LOSS_COOLDOWN', 'ZONE_ORIGIN_BOS_REQUIRED')]
        if waiting:
            p = waiting[0]
            lines += ['', f"👀 СПОСТЕРІГАЄМО • {setup_text(p)} • рівень {p.get('level', 0):.8g}"]

    # Daily-only statistics in Kyiv; keep full lifetime history in the journal.
    trades = []
    for trade in state.get('trades', []):
        closed = engine.timestamp(trade.get('closed_ts'), engine.timestamp(trade.get('closed_at'), 0))
        if closed and closed <= now and not trade.get('excluded_from_win_rate') and trade.get('net_r') is not None:
            trades.append({**trade, 'closed_ts': closed})
    daily = [t for t in trades if datetime.fromtimestamp(t['closed_ts'] / 1000,
             ZoneInfo(cfg.timezone)).date() == local.date()]
    stat = engine.statistics(daily)
    wr = f"{100 * stat['win_rate']:.1f}%" if daily else '—'
    net_r = sum(t['net_r'] for t in daily)
    net = f'{net_r:+.2f}R' if abs(net_r) > 1e-10 else '0.00R'
    lines += ['', f'📊 День: закрито {len(daily)} • WR {wr} • {net}']
    return '\n'.join(lines)


def split_message(message, limit=3800):
    """Keep every lifecycle result, splitting long Telegram reports by line."""
    pages, page = [], ''
    for line in message.splitlines():
        while len(line) > limit:
            if page:
                pages.append(page)
                page = ''
            pages.append(line[:limit])
            line = line[limit:]
        combined = page + ('\n' if page else '') + line
        if len(combined) > limit:
            pages.append(page)
            page = line
        else:
            page = combined
    if page:
        pages.append(page)
    return pages or ['']


def compact_waiting(message):
    """Apply the current small waiting card to unsent older reports too."""
    output=[];waiting=False;header=None
    for line in message.splitlines():
        stripped=line.strip()
        if stripped.startswith(('ОЧІКУЄМО ВХОДУ', '🟡 ОЧІКУЄМО ЛІМІТ')):
            waiting=True;header=None;output.append(line);continue
        if waiting and (stripped.startswith(('ЗАКРИТО','✅ ЗАКРИТІ','ІНШІ ЗМІНИ','СКАСУВАННЯ ЛІМІТІВ','НА СПОСТЕРЕЖЕННІ','🟠 СПОСТЕРІГАЄМО','У СУПРОВОДІ','🔵 СУПРОВІД','WR сигналів','v12.'))
                        or 'ЗАКРИТО:' in stripped):
            waiting=False;header=None
        if waiting:
            mark=re.match(r'^(#\S+)\s+(LONG|SHORT)\b',stripped)
            if mark:header=mark.groups()
            entry=re.search(r'Вхід:\s*([0-9.eE+\-]+)',stripped)
            if entry and header:
                output.append(f'{header[0]} {header[1]} • Вхід: {entry.group(1)}');header=None
            elif 'виконання НЕВИЗНАЧЕНЕ' in stripped:
                output.append(line)
            continue
        output.append(line)
    return '\n'.join(output)


def clean_queued_message(message):
    """Remove obsolete status/exit instructions without dropping closed results."""
    message=compact_waiting(message)
    queued_unverified = any(line.strip().startswith(('Дані не оновлені.', 'Перевірка даних:'))
                            for line in message.splitlines())
    output=[]
    have_close_header=False
    for line in message.splitlines():
        text=line.strip()
        if any(text.startswith(prefix) for prefix in (
            'Увага: широкий спред', 'Нові входи на паузі; супровід',
            'У СУПРОВОДІ — 0', 'Відкритих угод немає.',
            'Новий ордер — після звільнення',
            'Усі закриті угоди (історія всіх версій):',
            'Угоди, відкриті за ',
            'Останнє закриття зараховано до версії входу ',
            'Обраний сценарій: ОЧІКУВАННЯ',
            'Нових закриттів немає.',
            'Остання закрита:',
            'ATR 15m:', 'Ліквідність ↑',
            'Дані не оновлені. Збережений стан', 'Перевірка даних:',
            '⚡ СТАТУС УГОД ТА ОРДЕРІВ', 'СТАТУС УГОД ТА ОРДЕРІВ',
            '📊 СТАТИСТИКА BZU', 'СТАТИСТИКА BZU',
            'Тиждень: закрито',
            '↩️ імпульс із цієї зони', '↩ імпульс із цієї зони')):
            continue
        if text=='ЗАКРИТО З МИНУЛОГО ЗВІТУ':
            continue
        if text.startswith('Зони зайнято:'):
            match = re.search(r'Зони зайнято: (\d+/2) • OPEN (\d+) • PENDING (\d+)', text)
            if match:
                line = (f'📌 Місця: {match.group(1)} • 🔵 Супровід: {match.group(2)}'
                        f' • ⏳ Ліміти: {match.group(3)}'
                        + (' • ⚠️ не перевірено' if queued_unverified else ''))
        elif text.startswith('Відкриті угоди:'):
            # Checklist v12.22 included the same counters twice; retain only
            # the compact status bar when present.
            if 'Зони зайнято:' in message:
                continue
            match = re.search(r'Відкриті угоди: (\d+) • Ліміти в очікуванні: (\d+)', text)
            if match:
                line = f'🔵 Угоди: {match.group(1)} • ⏳ Ліміти: {match.group(2)}'
        elif text.startswith('День: закрито'):
            line = '📊 ' + text
        elif 'В ОЧІКУВАННІ ' in text:
            match = re.search(r'В ОЧІКУВАННІ (#[^ ]+) (LONG|SHORT) @ ([\d.eE+\-]+)', text)
            if match:
                line = f'🟡 {match.group(1)} {match.group(2)} • ліміт {match.group(3)}'
        elif text.startswith('Вхід:') and ' • до входу ' in text:
            match = re.search(r'SL: ([\d.eE+\-]+) • до входу ([\d.eE+\-]+) • (\d+) хв', text)
            if match:
                line = (f'🛡️ SL: {match.group(1)} • ↔ до входу: {match.group(2)}'
                        f' • ⏱ ще {match.group(3)} хв')
        if text.startswith('У СУПРОВОДІ — '):
            line='🔵 '+text.replace('У СУПРОВОДІ','СУПРОВІД',1)
        elif text.startswith('ОЧІКУЄМО ВХОДУ — '):
            line='🟡 '+text.replace('ОЧІКУЄМО ВХОДУ','ОЧІКУЄМО ЛІМІТ',1)
        elif text.startswith('НА СПОСТЕРЕЖЕННІ — '):
            line='🟠 '+text.replace('НА СПОСТЕРЕЖЕННІ','СПОСТЕРІГАЄМО',1)
        if text=='✅ ЗАКРИТІ УГОДИ':
            have_close_header=True
        elif 'ЗАКРИТО:' in text and not have_close_header:
            output.extend(['','✅ ЗАКРИТІ УГОДИ'])
            have_close_header=True
        line=re.sub(r' • Зайнято місць: \d+/2', '', line)
        line=re.sub(r'Залишок \d+% • ', '', line)
        line=re.sub(r'TP([123]) ([0-9.]+) \((?:закрити [0-9.]+%|досягнуто)\)', r'TP\1: \2',line)
        if line.strip():
            output.append(line)
        elif output and output[-1]:
            output.append('')
    return '\n'.join(output).strip()
