'use strict';

const $ = id => document.getElementById(id);
const OKX = 'https://www.okx.com/api/v5/market';
const STATE_MAX_AGE = 27 * 60_000;
const QUOTE_MAX_AGE = 15_000;
const chartFrames = { '5m': 300, '15m': 900 };
const time = new Intl.DateTimeFormat('uk-UA', {
  timeZone: 'Europe/Kyiv', hour: '2-digit', minute: '2-digit', second: '2-digit'
});
const position = new Map();
let state, quote, chart, series, chartRequest = () => {}, priceLines = [], frame = '15m';
let history = [], currentBar, lastSampleTime = 0, audio, sound = false, feedError = false;
let socket, socketAttempt = 0, socketPing;
let showAllZones = false, selectedZone = null;
let stateError = false, stateBusy = false, tickerBusy = false, candlesBusy = false;

const number = n => typeof n === 'number' && Number.isFinite(n);
const stamp = n => number(n) && n > 0 ? time.format(n) : '—';
const format = n => number(n) ? n.toFixed(Math.max(2, (String(state?.tick ?? .01).split('.')[1] ?? '').length)) : '—';
const sideLabel = side => side === 1 ? 'LONG' : 'SHORT';
const eventTime = ts => number(ts) ? new Intl.DateTimeFormat('uk-UA', {timeZone:'Europe/Kyiv',day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}).format(ts) : 'Час не передано';
const ready = now => state && !stateError && state.status === 'READY' &&
  state.snapshot_ts <= now + 5_000 && now - state.snapshot_ts < STATE_MAX_AGE;

async function json(url, timeout = 5000) {
  const response = await fetch(url, { cache: 'no-store', signal: AbortSignal.timeout(timeout) });
  if (!response.ok) throw new Error('HTTP ' + response.status);
  return response.json();
}

function td(text) {
  const cell = document.createElement('td');
  cell.textContent = text;
  return cell;
}

function notify(order, tracker, key, message) {
  if (tracker.events.has(key)) return;
  tracker.events.add(key);
  tracker.flash = Date.now();
  $('alert').textContent = sideLabel(order.side) + ' · ' + message;
  if (sound && audio?.state === 'running') {
    const oscillator = audio.createOscillator();
    const gain = audio.createGain();
    oscillator.frequency.value = ['SL','EXPIRED','CANCELLED'].includes(key) ? 330 : key.startsWith('TP') ? 740 : 520;
    oscillator.connect(gain);
    gain.connect(audio.destination);
    gain.gain.setValueAtTime(.12, audio.currentTime);
    gain.gain.exponentialRampToValueAtTime(.001, audio.currentTime + .28);
    oscillator.start();
    oscillator.stop(audio.currentTime + .30);
  }
}

function orderTracker(order) {
  const old = position.get(order.id);
  if (old) return old;
  const tracker = { events: new Set(), touch: false, tp1: false, end: null, lastTs: 0,
    observedStatus: order.status };
  position.set(order.id, tracker);
  return tracker;
}

function track(order, tracker, market, now) {
  const pending = order.status === 'PENDING';
  const open = order.status === 'OPEN';
  if (open && tracker.observedStatus !== 'OPEN') {
    // The new ledger resolves the pending order. Discard prior provisional exits.
    tracker.end = null;
    tracker.tp1 = false;
  }
  tracker.observedStatus = order.status;
  if (open && order.tp1_hit) tracker.tp1 = true;
  if (pending && number(order.expires_ts) && now >= order.expires_ts && !tracker.touch && !tracker.end) {
    tracker.end = 'EXPIRED';
    notify(order, tracker, 'EXPIRED', 'Минув час дії лімітки');
  }
  if (!market || !(pending || open) || tracker.end || market.ts <= tracker.lastTs ||
      (pending && market.ts < (order.effective_ts ?? state.snapshot_ts)) ||
      (open && number(order.filled_ts) && market.ts < order.filled_ts)) return;
  tracker.lastTs = market.ts;
  const move = order.side * (market.price - order.entry);
  if (pending && !tracker.touch) {
    if (move <= 0 && (!number(order.expires_ts) || market.ts < order.expires_ts)) {
      tracker.touch = true;
      notify(order, tracker, 'ENTRY', 'Дотик ціни до входу ' + format(order.entry));
    } else if (move > 0 && move <= .08000001) {
      notify(order, tracker, 'APPROACH', 'Ціна підійшла до лімітки ' + format(order.entry));
    }
  }
  if (!(open || tracker.touch)) return;
  if (order.side * (market.price - order.stop) <= 0) {
    tracker.end = 'SL';
    notify(order, tracker, 'SL', 'Ціна торкнулася SL ' + format(order.stop));
    return;
  }
  if (!tracker.tp1 && number(order.tp1) && order.side * (market.price - order.tp1) >= 0) {
    tracker.tp1 = true;
    const full = order.tp1_fraction === 1;
    if (full) tracker.end = 'TP1';
    notify(order, tracker, 'TP1', 'Ціна торкнулася TP1 ' + format(order.tp1) +
      (full ? ' · рівень повної фіксації' : ' · перевірте часткову фіксацію і рівень Б/У'));
  }
  if (!tracker.end && tracker.tp1 && number(order.tp2) && order.tp2 !== order.tp1 &&
      order.side * (market.price - order.tp2) >= 0) {
    tracker.end = 'TP2';
    notify(order, tracker, 'TP2', 'Ціна торкнулася TP2 ' + format(order.tp2));
  }
}

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text != null) node.textContent = text;
  if (className) node.className = className;
  return node;
}

const clamp = n => Math.max(0, Math.min(100, n));
const delta = (entry, price) => number(entry) && price > 0 ? entry - price : null;
function distanceText(entry, price) {
  const d = delta(entry, price);
  if (d === null) return 'Рівень ще не розраховано';
  return signed(d, ' $') + ' (' + signed(d * 100, '¢') + ') · ' + signed(d / price * 100, '%') +
    ' · ' + Math.round(Math.abs(d) / (state.tick || .01)) + ' тіків';
}
function scoreText(row) {
  return number(row.score) ? formatScore(row.score) + '/100' + (row.grade ? ' ' + row.grade : '') : 'Не оцінено';
}
const formatScore = n => Number(n.toFixed(1)).toString();
function statusText(row, price) {
  return ({ OPEN:'У супроводі', PENDING:'Ліміт активний', CANCELLING:'Скасування ліміту',
    WAIT_CONFIRMATION:'Підтвердження ' + (row.confirmation_timeframe || 'структури'),
    BLOCKED:'Вхід заблоковано', OBSERVING:'Спостереження' })[row.status] ??
    (Math.abs(row.entry - price) <= .08 ? 'Готується вхід · ціна поруч' : 'Кандидат · чекаємо підхід');
}
function radarRows(price) {
  const candidates = state.candidates ?? [];
  return [1, -1].map(side => {
    const onSide = row => row.side === side && number(row.entry) &&
      (side === 1 ? (row.low ?? row.entry) <= price : (row.high ?? row.entry) >= price);
    let rows = candidates.filter(row => onSide(row) && !['BLOCKED','CANCELLING'].includes(row.status));
    if (!rows.length) rows = state.zones.filter(row => onSide(row) && (!row.status || row.status === 'OBSERVING'));
    if (!rows.length) rows = candidates.filter(row => onSide(row) && row.status === 'BLOCKED');
    return rows.sort((a,b) => Math.abs(a.entry-price)-Math.abs(b.entry-price) || (b.score ?? 0)-(a.score ?? 0))[0] ?? null;
  });
}
function visibleZones(price = displayedPrice()) {
  if (showAllZones) return state.zones;
  const ids = new Set();
  for (const side of [1,-1]) {
    const rows=state.zones.filter(z=>z.side===side && (side===1 ? z.low<=price : z.high>=price));
    rows.sort((a,b)=>(a.status==='BLOCKED')-(b.status==='BLOCKED') ||
      Math.abs((a.entry ?? (a.low+a.high)/2)-price)-Math.abs((b.entry ?? (b.low+b.high)/2)-price));
    if (rows[0]) ids.add(rows[0].id);
  }
  return state.zones.filter(z => ids.has(z.id) || z.id === selectedZone ||
    state.orders.some(o => o.side === z.side && z.low <= o.entry && o.entry <= z.high));
}
function displayedPrice() {
  const now = Date.now();
  return quote && quote.ts <= now + 3000 && now - quote.ts <= QUOTE_MAX_AGE ? quote.price : state.price;
}
function renderZones(price) {
  const container = $('zones');
  container.replaceChildren();
  $('zone-count').textContent = state.zones.length + ' зон';
  $('all-zones').textContent = showAllZones ? 'Тільки робочі' : 'Усі зони (' + state.zones.length + ')';
  for (const z of state.zones) {
    const row = element('tr');
    if (z.id === selectedZone) row.classList.add('selected');
    const name = td('');
    name.append(element('strong', (z.timeframe ?? '') + ' ' + z.kind),
      element('small', sideLabel(z.side) + ' · ' + format(z.low) + '–' + format(z.high)));
    const badge = element('span', statusText(z, price), 'zone-badge');
    badge.dataset.status = z.status ?? 'OBSERVING';
    name.append(badge);
    name.title = z.reason_text ?? z.reason ?? 'Зона без оціненого сетапу';
    const score = td(scoreText(z));
    if (z.score_source==='PATTERN' && number(z.score)) score.append(element('small','Патерн'));
    score.title = z.score_source === 'PATTERN' ? 'Бал патерну; повна перевірка входу ще не пройдена' :
      z.score_source === 'UNASSESSED' ? 'Рушій ще не оцінив сетап для цієї зони' : 'Оцінка контексту рушієм';
    const entry = td('');
    entry.append(element('strong', format(z.entry)), element('small', distanceText(z.entry, price)),
      element('small', entrySource(z)));
    const action = td('');
    const focus = element('button', '◎');
    focus.type = 'button'; focus.dataset.zone = z.id;
    focus.dataset.focus = 'zone-' + z.id;
    focus.setAttribute('aria-label', 'Підсвітити ' + z.kind + ' ' + (z.timeframe ?? '') + ' ' + sideLabel(z.side));
    focus.setAttribute('aria-pressed', String(z.id === selectedZone));
    action.append(focus);
    row.append(name, score, entry, action); container.append(row);
  }
  if (!container.children.length) {
    const row = element('tr'), cell = td('Сканер ще не передав зон. Чекаємо наступний цикл.');
    cell.colSpan = 4; row.append(cell); container.append(row);
  }
}
function entrySource(row) {
  return ({ ORDER:'Ордер за журналом', PLANNER:'Розрахунок рушія', ANCHOR:'Попередній рівень сетапу',
    ZONE_BOUNDARY:'Орієнтир: межа зони' })[row.entry_source] ?? 'Рівень зі зліпка';
}
function copyButtons(row) {
  const wrap = element('div', null, 'copy-actions');
  for (const [key,label] of [['entry','Entry'], ['stop','SL'], ['tp1','TP1'], ['be_stop','Б/У']]) {
    if (!number(row[key])) continue;
    const button = element('button', 'Копіювати ' + label);
    button.type = 'button'; button.dataset.copy = String(row[key]);
    button.dataset.copyLabel = label;
    button.dataset.focus = 'copy-' + row.id + '-' + key;
    wrap.append(button);
  }
  return wrap;
}
function proximity(row, price) {
  const wrap = element('div');
  const gap = Math.abs(price-row.entry);
  // A fixed, visible 1% window keeps progress stable as the live quote moves.
  const windowSize = Math.max(Math.abs(row.entry) * .01, (state.tick || .01) * 20);
  const track = element('div', null, 'distance-track');
  const fill = element('div', null, 'distance-fill');
  fill.style.width = clamp(100*(1-gap/windowSize)) + '%'; track.append(fill);
  const caption = element('div', null, 'distance-caption');
  caption.append(element('span', 'До входу ' + distanceText(row.entry, price)),
    element('span', 'Вікно 1%'));
  wrap.append(track,caption);
  return wrap;
}
function lifecycle(order, price) {
  const wrap = element('div');
  const track = element('div', null, 'lifecycle');
  track.setAttribute('role','img');
  track.setAttribute('aria-label', 'Шлях ціни: SL ' + format(order.stop) + ', вхід ' + format(order.entry) +
    ', поріг Б/У ' + format(order.be_trigger) + ', TP1 ' + format(order.tp1) + ', TP2 ' + format(order.tp2) +
    ', поточна ціна ' + format(price) + '. Позначки етапів у рівних інтервалах.');
  track.append(element('div', null, 'lifecycle-track'));
  const milestones = [['SL',order.stop],['Вхід',order.entry],['Б/У',order.be_trigger],['TP1',order.tp1],['TP2',order.tp2]]
    .filter(([,value]) => number(value)).sort((a,b)=>order.side*(a[1]-b[1]));
  const groups = [];
  for (const [label,value] of milestones) {
    const same = groups.find(g => Math.abs(g.value-value)<state.tick/2);
    if (same) same.labels.push(label); else groups.push({value,labels:[label]});
  }
  let marker = 0;
  for (let i=0;i<groups.length;i++) {
    const group = groups[i], x = i/(groups.length-1)*100;
    if (order.side*(price-group.value)>=0) marker=x;
    if (i && order.side*(price-groups[i-1].value)>=0 && order.side*(price-group.value)<0)
      marker=(i-1+(price-groups[i-1].value)/(group.value-groups[i-1].value))/(groups.length-1)*100;
    const dot = element('div', null, 'milestone');
    dot.dataset.label = i===0 ? 'SL' : i===groups.length-1 ? 'TP2' : group.labels[0];
    dot.style.left = x + '%';
    const confirmed = group.labels.includes('TP1') ? order.tp1_hit :
      group.labels.includes('Вхід') ? true : group.labels.includes('Б/У') ? order.breakeven_armed : false;
    if (confirmed) dot.classList.add('reached');
    dot.append(element('span', group.labels.join(' / ')), element('span', format(group.value)));
    track.append(dot);
  }
  const live = element('div', format(price), 'live-marker');
  live.style.left = Math.max(5,Math.min(95,marker)) + '%'; track.append(live);
  wrap.append(track,element('p','Етапи у рівних інтервалах · ціна рухається між рівнями','muted micro'));
  return wrap;
}
function management(order, tracker, price, usable) {
  if (!usable) return 'Останній стан за журналом. Оновіть дані перед перевіркою позиції.';
  if (order.status==='CANCELLING') return 'Бот запросив скасування. Перевірте ордер і можливе виконання у брокера.';
  if (tracker.end) return ({ SL:'Ціна торкнулася SL. Перевірте закриття у брокера.',
    TP1:'Ціна торкнулася цілі повного виходу. Перевірте закриття у брокера.',
    TP2:'Ціна торкнулася TP2. Перевірте закриття залишку у брокера.',
    EXPIRED:'Строк лімітки минув. Очікуємо підтвердження журналу.' })[tracker.end];
  if (order.status==='PENDING') return tracker.touch ?
    'Ціна торкнулася Entry. Виконання ще не підтверджене журналом.' :
    'Чекаємо виконання лімітки. SL ' + format(order.stop) + ' · TP1 ' + format(order.tp1) + '.';
  if (order.tp1_hit) {
    const protectedStop = order.side*(order.stop-order.entry)>=0;
    return (protectedStop ? 'Стоп уже захищає вхід: ' : 'Поточний SL: ') + format(order.stop) +
      '. Залишок ' + Math.round((order.remaining ?? 1)*100) + '% → TP2 ' + format(order.tp2) +
      (number(order.pending_stop) ? '. Запланований SL ' + format(order.pending_stop) + ' з ' + stamp(order.pending_stop_effective_ts) : '');
  }
  if (tracker.tp1) return 'Дотик TP1. Перевірте фіксацію ' + Math.round(order.tp1_fraction*100) +
    '%' + (order.tp1_fraction<1 && number(order.be_stop) ? ' і перенесення SL на ' + format(order.be_stop) : '') + ' у брокера.';
  return 'Утримуємо за планом. SL ' + format(order.stop) + ' · до TP1 ' +
    format(Math.max(0,order.side*(order.tp1-price))) + ' $.';
}
function countdown(expiry) {
  if (!number(expiry)) return 'Строк не передано';
  const seconds=Math.ceil((expiry-Date.now())/1000);
  return seconds<=0 ? 'Строк минув' : 'Ще ' + Math.floor(seconds/60) + ' хв ' + String(seconds%60).padStart(2,'0') + ' с';
}
function eventText(event) {
  const label = ({ PLACED:'Ліміт виставлено', FILLED:'Вхід за журналом', CANCEL_REQUESTED:'Запит скасування',
    CANCELLED:'Ліміт скасовано', EXPIRED:'Строк ліміту минув', CLOSED:'Угоду закрито',
    TP1:'TP1', TP1_PARTIAL:'Часткова фіксація TP1', TP1_REACHED:'Досягнуто TP1', TP2:'Досягнуто TP2',
    PAPER_STOP_AMENDED:'Стоп оновлено', UNRESOLVED:'Виконання потребує перевірки' })[event.kind] ?? event.kind;
  return label + (event.side===1 || event.side===-1 ? ' · ' + sideLabel(event.side) : '') +
    (number(event.entry) ? ' ' + format(event.entry) : '') + (number(event.net_r) ? ' · ' + signed(event.net_r) + 'R' : '') +
    (event.reason_text ? ' · ' + event.reason_text : '');
}
function renderActivity() {
  const events=(state.recent_events ?? []).slice(-5).reverse();
  $('last-event').textContent = events.length ? 'Останнє рішення (' + eventTime(events[0].ts) + '): ' + eventText(events[0]) : 'У журналі ще немає подій.';
  $('events').replaceChildren(...events.map(e => {
    const item=element('li');
    item.append(element('time',eventTime(e.ts)),element('span',eventText(e)));
    return item;
  }));
}
function renderBanner(price, usable, live) {
  const active=state.orders.find(o=>o.status==='OPEN');
  const pending=state.orders.find(o=>o.status==='PENDING');
  const candidates=radarRows(price).filter(Boolean).sort((a,b)=>Math.abs(a.entry-price)-Math.abs(b.entry-price));
  const candidate=candidates[0];
  let mode='SCANNING', title='Скануємо ринок · шукаємо сетап', detail='Чекаємо підтверджених умов входу.', pill='СКАНУВАННЯ';
  if (candidate) {
    title='ЧЕКАЄМО ЗОНУ: ' + sideLabel(candidate.side) + ' ' +
      (number(candidate.low) ? '[' + format(candidate.low) + '–' + format(candidate.high) + ']' : format(candidate.entry));
    detail=(candidate.setup ?? candidate.kind) + ' · ' + statusText(candidate,price) + ' · Δ ' + distanceText(candidate.entry,price) +
      ' · ' + entrySource(candidate) + ' ' + format(candidate.entry) +
      (number(candidate.stop) ? ' | SL ' + format(candidate.stop) : '') +
      (number(candidate.tp1) ? ' | TP1 ' + format(candidate.tp1) : '');
  }
  if (pending) {
    const tracker=orderTracker(pending);
    mode='PENDING'; pill='ЛІМІТКА В ОЧІКУВАННІ';
    title='ЧЕКАЄМО ВХІД: LIMIT ' + sideLabel(pending.side) + ' @ ' + format(pending.entry);
    detail='Поточна ' + format(price) + ' · до входу ' + distanceText(pending.entry,price) +
      ' · ' + countdown(pending.expires_ts) + ' | SL ' + format(pending.stop) + ' | TP1 ' + format(pending.tp1);
    if (tracker.touch) detail='Дотик Entry · перевірте виконання у брокера. Журнал ще очікує лімітку.';
    if (tracker.end) detail=management(pending,tracker,price,usable);
  } else if (!active && state.orders.length) {
    mode='CANCELLING'; title='БОТ СКАСОВУЄ ЛІМІТКУ'; pill='ПЕРЕВІРКА ОРДЕРА';
    detail='Чекаємо підтвердження скасування за журналом.';
  }
  if (active) {
    const move=active.side*(price-active.entry);
    mode=move>=0 ? 'ACTIVE' : 'DRAWDOWN'; pill='СУПРОВІД ЗА ЖУРНАЛОМ';
    title='У СУПРОВОДІ: ' + sideLabel(active.side) + ' ВІД ' + format(active.entry) + ' · ' +
      signed(move, ' $/од.') + (active.risk>0 ? ' (' + signed(move/active.risk) + 'R)' : '');
    detail=management(active,orderTracker(active),price,usable);
    if (pending) detail += ' Також очікуємо ' + sideLabel(pending.side) + ' @ ' + format(pending.entry) + '.';
  }
  if (!usable) {
    mode=state.status==='DATA_HOLD' ? 'DATA_HOLD' : 'STALE'; pill='СИГНАЛИ ПРИЗУПИНЕНО';
    title=state.status==='DATA_HOLD' ? 'ПЕРЕВІРКА ДАНИХ · DATA HOLD' : 'СТАН БОТА ЗАСТАРІВ / НЕДОСТУПНИЙ';
    detail='Останній зліпок: ' + stamp(state.snapshot_ts) + '. Рівні нижче — останні відомі, сповіщення призупинено.';
  } else if (!live) detail += ' · Дистанції за ціною зі зліпка.';
  const banner=$('state-banner'); banner.dataset.mode=mode;
  banner.classList.toggle('near',usable && live && !active && pending && !orderTracker(pending).touch &&
    !orderTracker(pending).end && Math.abs(price-pending.entry)<=.08000001);
  $('state-title').textContent=title; $('state-detail').textContent=detail; $('state-pill').textContent=pill;
  $('state-eyebrow').textContent=banner.classList.contains('near') ? 'ЦІНА БІЛЯ ЛІМІТКИ · ПРИГОТУЙТЕСЯ ДО ПЕРЕВІРКИ ВХОДУ' : 'ПОТОЧНИЙ СТАН СИСТЕМИ';
  for (const [id,enabled] of [['step-scan',usable && !state.orders.length],['step-pending',usable && !active && !!pending],['step-active',usable && !!active]]) {
    $(id).classList.toggle('current',enabled);
    if (enabled) $(id).setAttribute('aria-current','step'); else $(id).removeAttribute('aria-current');
  }
}
function renderRadar(price, live) {
  $('radar-source').textContent=live ? 'Live Δ ціни' : 'Δ за зліпком';
  const container=$('radar'); container.replaceChildren();
  radarRows(price).forEach((row,index) => {
    const side=index===0 ? 1 : -1;
    const card=element('article',null,'candidate'); card.dataset.side=side;
    card.append(element('h3',sideLabel(side),side===1?'long':'short'));
    if (!row) {card.append(element('p','На цьому боці ще немає оціненого кандидата.','reason'));container.append(card);return;}
    card.append(element('p',(row.timeframe ?? '') + ' ' + (row.setup ?? row.kind ?? 'Сетап'),'setup'),
      element('p',format(row.entry),'candidate-entry'),element('p',entrySource(row),'muted'),
      element('p',distanceText(row.entry,price),'distance'));
    const badge=element('span',statusText(row,price),'badge');
    card.append(badge,element('p',scoreText(row) + (row.score_source==='PATTERN' ? ' · патерн' : ''),'reason'));
    if (number(row.stop)) card.append(element('p','SL ' + format(row.stop) + ' · TP1 ' + format(row.tp1),'reason'));
    if (row.reason_text) card.append(element('p',row.reason_text,'reason'));
    container.append(card);
  });
}
function render() {
  if (!state) return;
  const focusId=document.activeElement?.dataset.focus;
  const now=Date.now(), live=quote && quote.ts<=now+3000 && now-quote.ts<=QUOTE_MAX_AGE;
  const price=displayedPrice(), usable=ready(now);
  const market=usable && live && !document.hidden ? quote : null;
  $('price').textContent=format(price);
  $('quote-status').textContent=live ? 'Live · ' + stamp(quote.ts) : (feedError ? 'OKX недоступний' : 'Котирування застаріло') + ' · зліпок';
  $('stamp').textContent=stamp(state.snapshot_ts); $('instrument').textContent=state.instrument;
  $('regime').textContent=({BULLISH_ACTIVE:'Висхідний',BEARISH_ACTIVE:'Низхідний',RANGE:'Діапазон',PULLBACK_PHASE:'Відкат'})[state.market_regime] ?? 'Невідомий';
  $('state-status').textContent=usable ? 'Журнал актуальний' : state.status==='DATA_HOLD' ? 'Дані на перевірці' : 'Потрібен новий зліпок';
  $('capacity').textContent=(state.occupied_slots ?? state.orders.length) + '/2 місць';
  $('daily').textContent=state.daily ? state.daily.entries + ' уг. · ' + signed(state.daily.net_r) + 'R' : '—';
  $('order-count').textContent=state.orders.length;
  const cards=$('orders'); cards.replaceChildren();
  for (const order of [...state.orders].sort((a,b)=>(b.status==='OPEN')-(a.status==='OPEN'))) {
    const tracker=orderTracker(order);
    if (usable && !document.hidden) track(order,tracker,market,now);
    const pending=order.status==='PENDING';
    const move=order.side*(price-order.entry);
    const card=element('article',null,'order');
    card.dataset.tone=tracker.end==='SL' || order.status==='CANCELLING' ? 'danger' :
      tracker.tp1 ? 'target' : order.status==='OPEN' ? 'entry' : 'approach';
    if (now-tracker.flash<900) card.classList.add('pulse');
    card.append(element('h3',(order.number ? '#'+order.number+' · ' : '') + sideLabel(order.side) + ' · ' + order.type + ' @ ' + format(order.entry)),
      element('p',order.status==='OPEN' ? 'Позиція за журналом' : order.status==='CANCELLING' ? 'Скасування за журналом' : 'Лімітка за журналом','order-label'));
    if (order.status==='OPEN') card.append(element('p',signed(move,' $/од.') + (order.risk>0 ? ' · '+signed(move/order.risk)+'R' : ''), 'pnl '+(move>=0?'long':'short')),
      element('small',(live ? 'Live' : 'Зліпок') + ' · ціновий рух без комісій; грошовий PnL залежить від обсягу'), lifecycle(order,price));
    else card.append(proximity(order,price));
    card.append(element('p',management(order,tracker,price,usable),'action'));
    const details=element('dl');
    const rr=order.risk>0 && number(order.tp1) ? order.side*(order.tp1-order.entry)/order.risk : null;
    const fields=[['Сетап',order.setup ?? 'Не вказано'],['Бал / клас',scoreText(order)],['Entry / SL',format(order.entry)+' / '+format(order.stop)],
      ['TP1 / TP2',format(order.tp1)+' / '+format(order.tp2)],['Ризик ціни / R:R TP1',format(order.risk)+' $/од. / '+(number(rr)?rr.toFixed(2):'Не розраховано')],
      ['Поріг / стоп Б/У',format(order.be_trigger)+' / '+format(order.be_stop)]];
    if (number(order.pending_stop)) fields.push(['Запланований SL',format(order.pending_stop)+' з '+stamp(order.pending_stop_effective_ts)]);
    for (const [key,value] of fields) details.append(element('dt',key),element('dd',value));
    if (pending) {
      const value=element('dd',countdown(order.expires_ts)); value.dataset.expiry=order.expires_ts;
      details.append(element('dt','Строк лімітки'),value);
    }
    card.append(details,copyButtons(order)); cards.append(card);
  }
  if (!cards.children.length) {
    const empty=element('div',null,'empty-state');
    empty.append(element('strong','Скануємо ринок · без відкритої угоди'),element('p','Найближчі рівні й причина очікування — у радарі нижче.'));
    cards.append(empty);
  }
  renderBanner(price,usable,live); renderRadar(price,live); renderZones(price); renderActivity();
  drawLevels();
  if (focusId) [...document.querySelectorAll('[data-focus]')].find(n=>n.dataset.focus===focusId)?.focus({preventScroll:true});
}

function signed(value, suffix = '') {
  return number(value) ? (value >= 0 ? '+' : '−') + Math.abs(value).toFixed(2) + suffix : '—';
}

function initChart() {
  const lib = window.LightweightCharts;
  if (!lib) {
    $('chart-message').textContent = 'Бібліотека графіка недоступна. Рівні наведено нижче.';
    return;
  }
  chart = lib.createChart($('chart'), {
    autoSize: true,
    layout: { background: { color: '#101925' }, textColor: '#92a1b5', fontSize: 10 },
    rightPriceScale: { minimumWidth: 65, scaleMargins: {top:.13, bottom:.12} },
    grid: { vertLines: { color: '#22324a' }, horzLines: { color: '#22324a' } },
    timeScale: { timeVisible: true, secondsVisible: false, tickMarkFormatter: t => stamp(t * 1000).slice(0,5) },
    localization: { locale: 'uk-UA', timeFormatter: t => stamp(t * 1000) }
  });
  series = chart.addSeries(lib.CandlestickSeries, {
    upColor: '#4bd2a5', downColor: '#f17785', borderVisible: false,
    wickUpColor: '#4bd2a5', wickDownColor: '#f17785',
    autoscaleInfoProvider: original => {
      const info=original();
      if (!info || !state) return info;
      const values=state.orders.flatMap(o=>[o.entry,o.stop,o.tp1,o.tp2,o.be_trigger]).filter(number);
      const selected=state.zones.find(z=>z.id===selectedZone);
      if (selected) values.push(selected.low,selected.high);
      if (values.length) {
        info.priceRange.minValue=Math.min(info.priceRange.minValue,...values);
        info.priceRange.maxValue=Math.max(info.priceRange.maxValue,...values);
      }
      return info;
    }
  });
  const paneView = { renderer: () => ({ draw: target => {
    if (!state || !history.length) return;
    target.useMediaCoordinateSpace(({ context, mediaSize }) => {
      const scale = chart.timeScale();
      const occupiedLabels=[];
      for (const zone of visibleZones()) {
        const y1 = series.priceToCoordinate(zone.high), y2 = series.priceToCoordinate(zone.low);
        const floor = Math.floor((zone.known_ts ?? 0) / (chartFrames[frame] * 1000)) * chartFrames[frame];
        if (floor > (currentBar?.time ?? history.at(-1).time)) continue;
        const x = floor <= history[0].time ? scale.timeToCoordinate(history[0].time) : scale.timeToCoordinate(floor);
        if (y1 === null || y2 === null) continue;
        const left = Math.max(0, x ?? 0);
        if (left >= mediaSize.width-30) continue;
        context.fillStyle = zone.side === 1 ? 'rgba(56,195,159,.13)' : 'rgba(255,112,132,.13)';
        const top=Math.min(y1,y2), bottom=Math.max(y1,y2);
        context.fillRect(left, top, Math.max(0, mediaSize.width-left), Math.max(1,bottom-top));
        if (zone.id===selectedZone) {
          context.strokeStyle='#92dff0'; context.lineWidth=2;
          context.strokeRect(left+1,top,Math.max(0,mediaSize.width-left-2),Math.max(1,bottom-top));
        }
        const label=(zone.timeframe ?? '')+' '+zone.kind+' '+sideLabel(zone.side)+' ['+format(zone.low)+'–'+format(zone.high)+'] · '+scoreText(zone);
        let labelY=Math.max(16,Math.min(mediaSize.height-12,(top+bottom)/2));
        // Separate nearby labels so two overlapping zones remain identifiable.
        for (const y of occupiedLabels) if (Math.abs(labelY-y)<20) labelY=y+20;
        if (labelY>mediaSize.height-8 || bottom<0 || top>mediaSize.height) continue;
        occupiedLabels.push(labelY);
        context.save(); context.beginPath(); context.rect(0,0,mediaSize.width,mediaSize.height); context.clip();
        context.font='10px system-ui';
        const width=Math.min(context.measureText(label).width+12,mediaSize.width-left-10);
        context.fillStyle='#0e1a27e8'; context.fillRect(left+5,labelY-12,width,18);
        context.fillStyle=zone.side===1?'#8ce2c2':'#ffb0b9';
        context.fillText(label,left+11,labelY,Math.max(0,width-10)); context.restore();
      }
    });
  } }), zOrder: () => 'bottom' };
  series.attachPrimitive({ attached: ({ requestUpdate }) => { chartRequest = requestUpdate; },
    paneViews: () => [paneView] });
}

function drawLevels() {
  if (!series || !state) return;
  for (const line of priceLines) series.removePriceLine(line);
  priceLines=[];
  const style=window.LightweightCharts.LineStyle;
  const add=(price,title,color,dashed=true) => {
    if (!number(price)) return;
    priceLines.push(series.createPriceLine({price,title,color,lineWidth:1,
      lineStyle:dashed?style.Dashed:style.Solid,axisLabelVisible:true}));
  };
  const price=displayedPrice();
  for (const order of state.orders) {
    const prefix=(order.number ? '#'+order.number+' ' : '')+sideLabel(order.side)+' ';
    add(order.entry,prefix+(order.status==='OPEN'?'В УГОДІ':order.status==='CANCELLING'?'СКАСУВАННЯ':'LIMIT')+
      ' @ '+format(order.entry)+(order.status==='PENDING'?' · Δ '+signed(order.entry-price,' $'):''),'#55d6ad');
    add(order.stop,prefix+'SL @ '+format(order.stop)+' ('+signed(order.side*(order.stop-order.entry)/order.risk)+'R)','#ff8994',false);
    add(order.be_trigger,prefix+'УМОВА Б/У @ '+format(order.be_trigger),'#f4c76b');
    add(order.tp1,prefix+'TP1 · '+Math.round(order.tp1_fraction*100)+'% ('+signed(order.side*(order.tp1-order.entry)/order.risk)+'R)','#7db7ff');
    if (order.tp2!==order.tp1) add(order.tp2,prefix+'TP2 · залишок ('+signed(order.side*(order.tp2-order.entry)/order.risk)+'R)','#7db7ff');
  }
  // Zone titles belong inside the shaded band; axis labels mark the precise entry.
  for (const zone of visibleZones(price)) {
    if (state.orders.some(o=>o.id===zone.order_id)) continue;
    add(zone.entry,(zone.timeframe ?? '')+' '+zone.kind+' '+sideLabel(zone.side)+
      (zone.entry_source==='PLANNER'?' · КАНДИДАТ':' · ОРІЄНТИР'),zone.side===1?'#4c9f88':'#ad6577');
  }
  series.applyOptions({priceFormat:{type:'price',precision:Math.max(2,(String(state.tick).split('.')[1] ?? '').length),minMove:state.tick || .01}});
  chartRequest();
}

function setHistory(bars, fit = false) {
  if (!series || !bars.length) return;
  const combined = new Map(history.map(bar => [bar.time, bar]));
  for (const bar of bars) combined.set(bar.time, bar);
  history = [...combined.values()].sort((a,b) => a.time - b.time).slice(-180);
  const latest = history.at(-1);
  if (currentBar && latest.time > currentBar.time) currentBar = undefined;
  if (currentBar?.time === latest.time) {
    currentBar = { ...latest, high: Math.max(latest.high, currentBar.high), low: Math.min(latest.low, currentBar.low), close: currentBar.close };
    history[history.length - 1] = currentBar;
  }
  series.setData(currentBar?.time > latest.time ? [...history, currentBar] : history);
  if (fit) chart.timeScale().fitContent();
  chartRequest();
}

function sampleCandle(market) {
  if (!series || market.ts <= lastSampleTime) return;
  lastSampleTime = market.ts;
  const span = chartFrames[frame];
  const start = Math.floor(market.ts / (span * 1000)) * span;
  const previous = currentBar ?? history.at(-1);
  if (previous && start < previous.time) return;
  if (currentBar && start > currentBar.time) {
    if (history.at(-1)?.time === currentBar.time) history[history.length - 1] = currentBar;
    else history.push(currentBar);
  }
  const base = previous?.time === start ? previous :
    { time:start, open:market.price, high:market.price, low:market.price };
  currentBar = { ...base, high:Math.max(base.high, market.price),
    low:Math.min(base.low, market.price), close:market.price };
  series.update(currentBar);
}

async function loadCandles(fit = false) {
  if (!state || !series || candlesBusy) return;
  candlesBusy = true;
  const requestedFrame = frame, instrument = state.instrument;
  try {
    const body = await json(OKX + '/candles?' + new URLSearchParams({ instId:instrument, bar:requestedFrame, limit:'180' }));
    if (body.code !== '0' || !Array.isArray(body.data)) throw new Error('OKX candles');
    if (frame !== requestedFrame || state.instrument !== instrument) return;
    const bars = body.data.map(row => {
      const [ms, open, high, low, close] = row.slice(0, 5).map(Number);
      return { time:ms / 1000, open, high, low, close };
    }).filter(row => Object.values(row).every(n => number(n) && n > 0));
    setHistory(bars, fit);
    $('chart-message').textContent = '';
  } catch {
    $('chart-message').textContent = history.length ? 'Історія зі зліпка і поточні котирування.' : 'Історія свічок тимчасово недоступна.';
  } finally {
    candlesBusy = false;
  }
}

function acceptTicker(row) {
  const price = Number(row?.last), ts = Number(row?.ts);
  if (row?.instId !== state?.instrument || !number(price) || price <= 0 ||
      !number(ts) || ts <= 0 || ts > Date.now() + 3000 || (quote && ts < quote.ts)) return false;
  quote = { price, ts };
  feedError = false;
  if (Date.now() - ts <= QUOTE_MAX_AGE && !document.hidden) sampleCandle(quote);
  return true;
}

function disconnectSocket() {
  clearInterval(socketPing);
  socket?.close();
  socket = undefined;
}

function socketFallback(instrument) {
  if (socket || Date.now() - socketAttempt < 15_000) return;
  socketAttempt = Date.now();
  try {
    const connection = new WebSocket('wss://ws.okx.com:8443/ws/v5/public');
    socket = connection;
    connection.addEventListener('open', () => {
      connection.send(JSON.stringify({ op:'subscribe', args:[{ channel:'tickers', instId:instrument }] }));
      socketPing = setInterval(() => { if (connection.readyState === WebSocket.OPEN) connection.send('ping'); }, 20_000);
    });
    connection.addEventListener('message', event => {
      if (event.data === 'pong' || connection !== socket) return;
      try { if (acceptTicker(JSON.parse(event.data)?.data?.[0])) render(); } catch {}
    });
    connection.addEventListener('error', () => connection.close());
    connection.addEventListener('close', () => {
      if (connection === socket) {
        clearInterval(socketPing);
        socket = undefined;
      }
    });
  } catch { socket = undefined; }
}

async function pollTicker() {
  if (!state || tickerBusy || document.hidden) return;
  tickerBusy = true;
  const instrument = state.instrument;
  try {
    const body = await json(OKX + '/ticker?' + new URLSearchParams({ instId:instrument }), 2700);
    if (instrument === state.instrument && (body.code !== '0' || !acceptTicker(body.data?.[0]) ||
        Date.now() - quote.ts > QUOTE_MAX_AGE)) throw new Error('OKX ticker');
  } catch {
    feedError = !quote || Date.now() - quote.ts > QUOTE_MAX_AGE;
    if (feedError) socketFallback(state.instrument);
  } finally {
    tickerBusy = false;
    render();
  }
}

async function pollState() {
  if (stateBusy || document.hidden) return;
  stateBusy = true;
  try {
    const next = await json('./live_state.json');
    if (next.schema !== 1 || !Array.isArray(next.orders) || !Array.isArray(next.zones) ||
        !number(next.snapshot_ts) || !number(next.price) || next.price<=0 || !/^[A-Z0-9-]{3,35}$/.test(next.instrument)) throw new Error('Invalid terminal snapshot');
    if (state && next.snapshot_ts < state.snapshot_ts) return;
    stateError = false;
    if (state && next.snapshot_ts === state.snapshot_ts) {
      await loadCandles();
      render();
      return;
    }
    const first = !state || next.instrument !== state.instrument;
    if (first) {
      quote = undefined; lastSampleTime = 0; history = []; currentBar = undefined;
      if (series) series.setData([]);
      position.clear(); selectedZone=null;
      disconnectSocket();
    }
    const previousSnapshot = state?.snapshot_ts;
    state = next;
    if (!first) {
      for (const event of next.recent_events ?? []) {
        const tracker = position.get(event.order_id);
        if (tracker && ['CANCELLED','EXPIRED'].includes(event.kind) && event.ts >= previousSnapshot) {
          notify(event, tracker, event.kind, (event.kind === 'CANCELLED' ?
            'Бот скасував лімітку ' : 'Час дії лімітки минув ') + format(event.entry));
        }
      }
    }
    const ids = new Set(next.orders.map(order => order.id));
    for (const id of position.keys()) if (!ids.has(id)) position.delete(id);
    setHistory(next.candles?.[frame] ?? [], first);
    render();
    void pollTicker();
    await loadCandles(first);
  } catch {
    stateError = true;
    $('state-status').textContent = 'Зліпок бота недоступний';
    if (!state) {
      $('state-banner').dataset.mode='DATA_HOLD'; $('state-title').textContent='СТАН БОТА НЕДОСТУПНИЙ';
      $('state-detail').textContent='Не вдалося завантажити live_state.json. Повторна спроба через 30 с.';
      $('state-pill').textContent='НЕМАЄ ЗЛІПКА';
    }
    render();
  } finally { stateBusy = false; }
}

$('audio').addEventListener('click', async () => {
  try {
    if (!audio) audio = new (window.AudioContext || window.webkitAudioContext)();
    if (audio.state !== 'running') await audio.resume();
    sound = !sound;
    $('audio').setAttribute('aria-pressed', String(sound));
    $('audio').textContent = sound ? 'Звук: увімк.' : 'Звук: вимк.';
  } catch { $('audio').textContent = 'Звук недоступний'; }
});

for (const key of Object.keys(chartFrames)) $(key).addEventListener('click', async () => {
  if (key === frame) return;
  frame = key; history = []; currentBar = undefined; lastSampleTime = 0;
  if (series) series.setData([]);
  $('5m').setAttribute('aria-pressed', String(frame === '5m'));
  $('15m').setAttribute('aria-pressed', String(frame === '15m'));
  setHistory(state?.candles?.[frame] ?? [], true);
  await loadCandles(true);
});

$('all-zones').addEventListener('click', () => {
  showAllZones=!showAllZones;
  $('all-zones').setAttribute('aria-pressed',String(showAllZones));
  render();
});
$('zones').addEventListener('click', event => {
  const button=event.target.closest('button[data-zone]');
  if (!button) return;
  selectedZone=selectedZone===button.dataset.zone ? null : button.dataset.zone;
  chart?.priceScale('right').applyOptions({autoScale:true});
  render();
  if (window.innerWidth<=760) $('chart-title').scrollIntoView({block:'center',behavior:'smooth'});
});
$('orders').addEventListener('click', async event => {
  const button=event.target.closest('button[data-copy]');
  if (!button) return;
  const value=format(Number(button.dataset.copy));
  try {
    await navigator.clipboard.writeText(value);
    $('copy-status').textContent=button.dataset.copyLabel+' '+value+' скопійовано';
  } catch {
    const field=element('textarea',value); field.style.position='fixed';field.style.top='-100px';document.body.append(field);
    field.select();
    try { $('copy-status').textContent=document.execCommand('copy') ? button.dataset.copyLabel+' '+value+' скопійовано' : 'Скопіюйте рівень вручну: '+value; }
    catch { $('copy-status').textContent='Скопіюйте рівень вручну: '+value; }
    field.remove();button.focus();
  }
});
initChart();
void pollState();
setInterval(() => {
  if (!state || document.hidden) return;
  for (const node of document.querySelectorAll('[data-expiry]')) node.textContent=countdown(Number(node.dataset.expiry));
  const price=displayedPrice(), now=Date.now();
  renderBanner(price,ready(now),quote && now-quote.ts<=QUOTE_MAX_AGE && quote.ts<=now+3000);
},1000);
setInterval(pollTicker, 3000);
setInterval(pollState, 30_000);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) { void pollState(); void pollTicker(); }
});
