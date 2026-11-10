"""Short Ukrainian lifecycle reports. Formatting never changes trading state."""
from __future__ import annotations

import re
from datetime import datetime
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
    if source == 'BREAKER_FLIP_15M':
        return 'Breaker • ретест пробою'
    if source == 'MOMENTUM_STAIRCASE_15M':
        return 'Сходинки тренду'
    name = plan.get('setup', plan.get('setup_type', ''))
    return SETUP_LABELS.get(name, name or 'Сетап')


def targets(plan):
    parts=[]
    for name in ('TP1','TP2','TP3'):
        level=plan.get(name.lower(),plan.get('tp') if name=='TP1' else None)
        parts.append(f'{name}: {level:.8g}' if level is not None else f'{name}: —')
    return ' • '.join(parts)


def full_exit_text(plan):
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
    reason = {'STOP': 'стоп', 'STOP_GAP': 'стоп із розривом ціни',
              'TAKE_PROFIT': 'тейк-профіт',
              'CLOSED_ROUND_TRIP_BE': 'повернення до входу після піку',
              'CLOSED_STALE_TIMEOUT': 'тайм-стоп на ретесті входу',
              'CLOSED_ADVERSE_CHOCH': 'ретест після зустрічного CHoCH',
              'CLOSED_STALE_REPLACEMENT': 'заміна на підтверджений A+', 'ADAPTIVE_FULL_EXIT':'повний вихід за зміною умов', 'INTRADAY_TIME_EXIT': 'за часом'}.get(
                  trade.get('close_reason', trade.get('reason')), 'завершення')
    detail = (', '.join(hits) + ' досягнуто; ' if hits else '') if trade.get('exit_policy') != 'SINGLE_FULL_EXIT' else ''
    if trade.get('ambiguous_ohlc'):
        reason += '; порядок SL/TP невідомий, враховано стоп'
    return detail + reason


def position_diagnostics(position, now, cfg, engine):
    view = position_monitor.guard(position, now)
    mins = view['time_in_trade_ms'] // 60_000
    time = f'{mins // 60} год {mins % 60} хв'
    entry = f"{position['entry']:.8g}"
    mode = view['mode']
    if mode == 'POST_TP1':
        detail = 'TP1 зафіксовано. Залишок ведемо за штатним стопом до TP2.'
    elif mode == 'ADVERSE_CHOCH':
        detail = f'CHoCH проти позиції. Вихід при ретесті {entry} у межах −0.10R…0R.'
    elif mode == 'ROUND_TRIP':
        detail = f'Round-trip активний. Вихід при поверненні до {entry} у межах ±0.10R.'
    elif mode == 'STALE_TIMEOUT':
        stop = f" Захисний SL {view['stale_stop']:.8g}." if view['stale_stop'] else ''
        detail = f'Тайм-стоп активний. Вихід на ретесті {entry}.{stop}'
    else:
        trip = engine.report_deadline(view['round_trip_at'], now, cfg) if view['round_trip_at'] > now else 'поріг 90 хв пройдено'
        stale = engine.report_deadline(view['stale_at'], now, cfg)
        detail = (f'Стоп чинний. Round-trip при піку +0.55R і ретесті {entry} після 90 хв ({trip}). '
                  f'Тайм-стоп від {stale} при піку нижче +0.60R.')
    return [f"⏱ {time} • MFE {view['max_favorable_r']:+.2f}R • MAE {(-view['max_adverse_r']):.2f}R", detail]


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
        lines += [f'Заміна ліміту: {old_label} → {new_label}; в одному циклі, без третього ордера.']
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
        lines += [f'Результат зараз ≈ {estimate}', *position_diagnostics(p, now, cfg, engine)]
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
        if not p.get('tp1_decision_gate'):
            lines += [f"Супровід до {engine.report_deadline(p['exit_deadline'], now, cfg)}"]

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
            if reason in ('DAILY_RISK_LIMIT_REACHED','LEVEL_PASSED_WHILE_UNARMED','EXECUTION_CONTROL_REVERSED','CAPITULATION_RECOVERY_LOST','PORTFOLIO_DIRECTION_CONFLICT','BETTER_OPPOSING_SCENARIO','LEGACY_PENDING_EXCESS_CAP_2'):
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
    if not active and not pending and engine.occupied_slots(state) < 2 and not health and not frozen and price_ok:
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
    today = local.date()
    daily = []
    for trade in state.get('trades', []):
        try:
            closed_ms = int(trade.get('closed_ts') or 0)
            if not (0 < closed_ms <= now) or trade.get('net_r') is None:
                continue
            closed_day = datetime.fromtimestamp(closed_ms / 1000, ZoneInfo(cfg.timezone)).date()
            if closed_day == today:
                daily.append(trade)
        except (ValueError, TypeError, OverflowError, OSError):
            # Historical incomplete records are not completed trades.
            continue
    total_r = engine.statistics(daily)['net_r']
    result_r = f'{total_r:+.2f}R' if abs(total_r) > 1e-10 else '0.00R'
    count = len(daily)
    noun = ('закрита угода' if count % 10 == 1 and count % 100 != 11
            else 'закриті угоди' if count % 10 in (2, 3, 4) and count % 100 not in (12, 13, 14)
            else 'закритих угод')
    lines += ['', f'Сьогодні (Київ): {count} {noun} • результат {result_r}']
    return '\n'.join(lines)


def build_checklist(state, snapshot, result, cfg, engine):
    """Compact v12.22 Telegram report. Presentation only, never mutates the ledger."""
    import directional_policy
    import ict_methodology

    now = snapshot.now
    local = datetime.fromtimestamp(now / 1000, ZoneInfo(cfg.timezone))
    session = ict_methodology.session_liquidity(snapshot)
    regime = directional_policy.market_regime(snapshot, cfg.tick, engine)
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
        f'💰 Ціна: {price} • {session["session"]} • 1H/4H: {trend}',
    ]

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
        elif kind == 'TP1_PARTIAL':
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
        lines += position_diagnostics(p, now, cfg, engine)
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
    if not pending and not active and not frozen and result.get('analysis', {}).get('entry_mode') in ('LIVE_REACTION','HYBRID'):
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
    if not pending and not active and not frozen and not selected and result.get('analysis', {}).get('entry_mode') not in ('LIVE_REACTION','HYBRID'):
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
