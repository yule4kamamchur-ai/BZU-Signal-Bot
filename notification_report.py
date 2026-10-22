"""Short Ukrainian lifecycle reports. Formatting never changes trading state."""
from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

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
              'TAKE_PROFIT': 'тейк-профіт', 'ADAPTIVE_FULL_EXIT':'повний вихід за зміною умов', 'INTRADAY_TIME_EXIT': 'за часом'}.get(
                  trade.get('close_reason', trade.get('reason')), 'завершення')
    detail = (', '.join(hits) + ' досягнуто; ' if hits else '') if trade.get('exit_policy') != 'SINGLE_FULL_EXIT' else ''
    if trade.get('ambiguous_ohlc'):
        reason += '; порядок SL/TP невідомий, враховано стоп'
    return detail + reason


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
    # Explain why no third signal or counter-trend limit is sent this cycle.
    direction = result.get('analysis', {}).get('direction_policy', {})
    if direction and not frozen:
        bias = {1:'LONG',-1:'SHORT',0:'ОЧІКУВАННЯ'}.get(direction.get('side'),'ОЧІКУВАННЯ')
        why = engine.REASON_UA.get(direction.get('reason'), direction.get('reason',''))
        lines += [f'Обраний сценарій: {bias}' + (f' • {why}' if why else '')]

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
        lines += ['', f'У СУПРОВОДІ — {len(active)}']
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
        lines += [f'Результат зараз ≈ {estimate}']
        if p.get('pending_target'):
            amendment=p['pending_target']
            lines += [f"Ціль {amendment['name']} з {engine.report_deadline(amendment['effective_ts'],now,cfg)}"]
        if p.get('pending_stop'):
            amendment = p['pending_stop']
            lines += [f"Новий SL {amendment['price']:.8g} з {engine.report_deadline(amendment['effective_ts'], now, cfg)}"]
        if not p.get('tp1_decision_gate'):
            lines += [f"Супровід до {engine.report_deadline(p['exit_deadline'], now, cfg)}"]

    if pending:
        lines += ['', f'ОЧІКУЄМО ВХОДУ — {len(pending)}']
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

    lines += ['', 'ЗАКРИТО З МИНУЛОГО ЗВІТУ']
    events = sorted(result.get('events', []), key=lambda e: e['ts'])
    closed = {e['order_id']: e for e in events if e['kind'] == 'CLOSED'}
    if not closed:
        lines += ['Нових закриттів немає.']
        previous = max(state.get('trades', []), key=lambda t: t.get('closed_ts', 0), default=None)
        if previous:
            lines += [f"Остання закрита: {engine.display_order(state, previous['id'])} • "
                      f"{engine.report_deadline(previous['closed_ts'], now, cfg)} • "
                      f"{previous['net_r']:+.2f}R ({close_reason(previous)})"]
    for order_id, event in closed.items():
        trade = next((t for t in reversed(state.get('trades', [])) if t['id'] == order_id), {})
        value = event['net_r']
        outcome = 'прибуток' if value > 0 else 'збиток' if value < 0 else 'беззбитково'
        lines += [f"{engine.display_order(state, order_id)} ЗАКРИТО: {value:+.2f}R — {outcome} • "
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
    if not pending and engine.occupied_slots(state) < 2 and not health and not frozen and price_ok:
        analysis = result.get('analysis', {})
        rows = analysis.get('watch', []) or analysis.get('setup_matrix', [])
        live = engine.open_plans(state)
        live_keys = {(p.get('setup'), scanner.timeframe(p)) for p in live}
        rows = [r for r in rows if (r.get('setup'), r.get('timeframe', '15m')) not in live_keys]
        best = scanner.best_scenario(rows, snapshot.price, None)
        if best and best.get('level') and best.get('side') in (-1, 1):
            side = 'LONG' if best['side'] == 1 else 'SHORT'
            ready = best.get('readiness_pct', 0) or 0
            lines += ['', 'НА СПОСТЕРЕЖЕННІ — ордер ще не активований',
                      f"{side} • {best.get('timeframe', '15m').lower()} • {setup_text(best)}",
                      f"Рівень: {best['level']:.8g} • готовність умов {ready:.0f}%",
                      candidate_reason(best, cfg, engine)]
    if result.get('action') in ('DAILY_PAUSE', 'SESSION_CLOSED', 'DIRECTION_CONFLICT'):
        lines += ['', engine.REASON_UA.get(result['action'], 'Нові входи на паузі') + '.']
    stats = engine.statistics_by_entry_version(state.get('trades', []))
    stat = stats.get(engine.VERSION, {})
    if stat.get('trades'):
        version_label = engine.VERSION.split('-')[2]
        lines += ['', f"WR сигналів {version_label}: {stat['win_rate']*100:.1f}% ({stat['wins']}/{stat['trades']}) • "
                  f"{stat['net_r']:+.2f}R"]
    elif state.get('trades'):
        last=max(state['trades'],key=lambda t:t.get('closed_ts',0))
        previous_version=engine.trade_entry_version(last)
        previous=stats[previous_version]
        label=previous_version.split('-')[2] if previous_version.startswith('full-ict-') else previous_version
        lines += ['',f"{engine.REPORT_RELEASE}: закритих угод ще немає.",
                  f"Попередня {label}: WR {previous['win_rate']*100:.1f}% ({previous['wins']}/{previous['trades']})."]
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
        if stripped.startswith('ОЧІКУЄМО ВХОДУ'):
            waiting=True;header=None;output.append(line);continue
        if waiting and (stripped.startswith(('ЗАКРИТО','ІНШІ ЗМІНИ','СКАСУВАННЯ ЛІМІТІВ','НА СПОСТЕРЕЖЕННІ','У СУПРОВОДІ','WR сигналів','v12.'))
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
    output=[]
    for line in message.splitlines():
        text=line.strip()
        if any(text.startswith(prefix) for prefix in (
            'Увага: широкий спред', 'Нові входи на паузі; супровід',
            'У СУПРОВОДІ — 0', 'Відкритих угод немає.',
            'Новий ордер — після звільнення')):
            continue
        line=re.sub(r' • Зайнято місць: \d+/2', '', line)
        line=re.sub(r'Залишок \d+% • ', '', line)
        line=re.sub(r'TP([123]) ([0-9.]+) \((?:закрити [0-9.]+%|досягнуто)\)', r'TP\1: \2',line)
        if line.strip():
            output.append(line)
        elif output and output[-1]:
            output.append('')
    return '\n'.join(output).strip()
