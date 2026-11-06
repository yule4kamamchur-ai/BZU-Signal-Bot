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
let stateError = false, stateBusy = false, tickerBusy = false, candlesBusy = false;

const number = n => typeof n === 'number' && Number.isFinite(n);
const stamp = n => number(n) && n > 0 ? time.format(n) : '—';
const format = n => number(n) ? n.toFixed(Math.max(2, (String(state?.tick ?? .01).split('.')[1] ?? '').length)) : '—';
const sideLabel = side => side === 1 ? 'LONG' : 'SHORT';
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

function renderZones() {
  const container = $('zones');
  container.replaceChildren();
  for (const z of state.zones) {
    const row = document.createElement('tr');
    row.append(td(z.kind), td(z.timeframe ?? '—'), td(sideLabel(z.side)),
      td(format(z.low) + '–' + format(z.high)), td(number(z.score) ? z.score + (z.grade ? ' · ' + z.grade : '') : '—'));
    container.append(row);
  }
  if (!container.children.length) {
    const row = document.createElement('tr');
    const cell = td('У зліпку немає активних зон');
    cell.colSpan = 5;
    row.append(cell);
    container.append(row);
  }
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

function render() {
  if (!state) return;
  const now = Date.now();
  const freshQuote = quote && quote.ts <= now + 3_000 && now - quote.ts <= QUOTE_MAX_AGE;
  const market = ready(now) && freshQuote && !document.hidden ? quote : null;
  $('price').textContent = format(freshQuote ? quote.price : state.price);
  $('quote-status').textContent = freshQuote ? 'Live · ' + stamp(quote.ts) :
    (feedError ? 'OKX недоступний' : 'Котирування застаріло') + ' · ціна зі зліпка';
  $('stamp').textContent = stamp(state.snapshot_ts);
  $('instrument').textContent = state.instrument;
  $('regime').textContent = ({ BULLISH_ACTIVE:'Висхідний', BEARISH_ACTIVE:'Низхідний',
    RANGE:'Діапазон', PULLBACK_PHASE:'Відкат' })[state.market_regime] ?? 'Невідомий';
  $('state-status').textContent = ready(now) ? 'Журнал актуальний' :
    state.status === 'DATA_HOLD' ? 'Дані на перевірці · сповіщення призупинено' :
    'Зліпок недоступний або застарів';
  $('capacity').textContent = (state.occupied_slots ?? state.orders.length) + '/2 місць';
  $('daily').textContent = state.daily ? state.daily.entries + ' уг. · ' + signed(state.daily.net_r) + 'R' : '—';
  const cards = $('orders');
  cards.replaceChildren();
  for (const order of state.orders) {
    const tracker = orderTracker(order);
    if (ready(now) && !document.hidden) track(order, tracker, market, now);
    const pending = order.status === 'PENDING';
    const move = market ? order.side * (market.price - order.entry) : null;
    let label = pending ? 'Очікує дотику' : order.status === 'OPEN' ? 'В позиції за журналом' : 'Скасування за журналом';
    let tone = order.status === 'OPEN' ? 'entry' : '';
    if (pending && move > 0 && move <= .08000001) [label, tone] = ['Готується вхід', 'approach'];
    if (pending && tracker.touch) [label, tone] = ['Дотик Entry · перевірте виконання', 'entry'];
    if (tracker.tp1) [label, tone] = [order.tp1_hit ? 'TP1 за журналом' : 'Дотик TP1 · перевірте виконання', 'target'];
    if (tracker.end === 'TP2') [label, tone] = ['Дотик TP2 · перевірте виконання', 'target'];
    if (tracker.end === 'SL') [label, tone] = ['Дотик SL · перевірте виконання', 'danger'];
    if (tracker.end === 'EXPIRED') [label, tone] = ['Минув час дії', 'danger'];
    if (order.status === 'CANCELLING') [label, tone] = ['Скасування за журналом', 'danger'];
    const card = document.createElement('article');
    card.className = 'order';
    card.dataset.tone = tone;
    if (now - tracker.flash < 900) card.classList.add('pulse');
    const heading = document.createElement('h3');
    heading.textContent = [order.number ? '#' + order.number : '', sideLabel(order.side), order.type].filter(Boolean).join(' · ');
    const status = document.createElement('p');
    status.textContent = label;
    const details = document.createElement('dl');
    const pnl = (order.status === 'OPEN' || tracker.touch) && !tracker.end && market && order.risk > 0 ?
      signed(move, ' $') + ' | ' + signed(move / order.risk) + 'R' : '—';
    for (const [key, value] of [['Сетап', order.setup ?? '—'], ['Бал / клас', [order.score, order.grade].filter(v => v != null).join(' / ') || '—'],
      ['Entry', format(order.entry)], ['SL', format(order.stop)], ['TP1', format(order.tp1)], ['TP2', format(order.tp2)],
      ['Поріг Б/У', format(order.be_trigger)], ['Стоп Б/У', format(order.be_stop)], ['Поріг трейлінгу', format(order.structural_trail_trigger)],
      ['Новий SL', format(order.pending_stop)], ['Діє до · Київ', pending ? stamp(order.expires_ts) : '—'], ['Рух від Entry', pnl]]) {
      const title = document.createElement('dt'), item = document.createElement('dd');
      title.textContent = key; item.textContent = value;
      details.append(title, item);
    }
    card.append(heading, status, details);
    cards.append(card);
  }
  if (!cards.children.length) cards.textContent = 'У зліпку немає лімітів або позицій.';
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
    layout: { background: { color: '#121d2f' }, textColor: '#b8cbdd' },
    grid: { vertLines: { color: '#22324a' }, horzLines: { color: '#22324a' } },
    timeScale: { timeVisible: true, secondsVisible: false, tickMarkFormatter: t => stamp(t * 1000).slice(0,5) },
    localization: { locale: 'uk-UA', timeFormatter: t => stamp(t * 1000) }
  });
  series = chart.addSeries(lib.CandlestickSeries, {
    upColor: '#4bd2a5', downColor: '#f17785', borderVisible: false,
    wickUpColor: '#4bd2a5', wickDownColor: '#f17785'
  });
  const paneView = { renderer: () => ({ draw: target => {
    if (!state || !history.length) return;
    target.useMediaCoordinateSpace(({ context, mediaSize }) => {
      const scale = chart.timeScale();
      for (const zone of state.zones) {
        const y1 = series.priceToCoordinate(zone.high), y2 = series.priceToCoordinate(zone.low);
        const floor = Math.floor((zone.known_ts ?? 0) / (chartFrames[frame] * 1000)) * chartFrames[frame];
        if (floor > (currentBar?.time ?? history.at(-1).time)) continue;
        const x = floor <= history[0].time ? scale.timeToCoordinate(history[0].time) : scale.timeToCoordinate(floor);
        if (y1 === null || y2 === null) continue;
        const left = Math.max(0, x ?? 0);
        context.fillStyle = zone.side === 1 ? 'rgba(56,195,159,.13)' : 'rgba(255,112,132,.13)';
        context.fillRect(left, Math.min(y1, y2), Math.max(0, mediaSize.width - left), Math.max(1, Math.abs(y2-y1)));
      }
    });
  } }), zOrder: () => 'bottom' };
  series.attachPrimitive({ attached: ({ requestUpdate }) => { chartRequest = requestUpdate; },
    paneViews: () => [paneView] });
}

function drawLevels() {
  if (!series || !state) return;
  for (const line of priceLines) series.removePriceLine(line);
  priceLines = [];
  for (const order of state.orders) {
    const label = order.number ? '#' + order.number + ' ' : '';
    for (const [key, title, color] of [['entry','Entry','#51d3aa'],['stop','SL','#ff8390'],
      ['tp1','TP1','#68acff'],['tp2','TP2','#68acff']]) {
      if (!number(order[key]) || (key === 'tp2' && order.tp2 === order.tp1)) continue;
      priceLines.push(series.createPriceLine({ price: order[key], title: label + title,
        color, lineWidth: 1, lineStyle: window.LightweightCharts.LineStyle.Dashed }));
    }
  }
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
        !number(next.snapshot_ts) || !/^[A-Z0-9-]{3,35}$/.test(next.instrument)) throw new Error('Invalid terminal snapshot');
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
      position.clear();
      disconnectSocket();
    }
    const previousSnapshot = state?.snapshot_ts;
    state = next;
    if (!first) {
      for (const event of next.recent_events ?? []) {
        const tracker = position.get(event.order_id);
        if (tracker && event.ts >= previousSnapshot) {
          notify(event, tracker, event.kind, (event.kind === 'CANCELLED' ?
            'Бот скасував лімітку ' : 'Час дії лімітки минув ') + format(event.entry));
        }
      }
    }
    const ids = new Set(next.orders.map(order => order.id));
    for (const id of position.keys()) if (!ids.has(id)) position.delete(id);
    renderZones();
    drawLevels();
    setHistory(next.candles?.[frame] ?? [], first);
    render();
    void pollTicker();
    await loadCandles(first);
  } catch {
    stateError = true;
    $('state-status').textContent = 'Зліпок бота недоступний';
    render();
  } finally { stateBusy = false; }
}

$('audio').addEventListener('click', async () => {
  try {
    if (!audio) audio = new (window.AudioContext || window.webkitAudioContext)();
    if (audio.state !== 'running') await audio.resume();
    sound = !sound;
    $('audio').setAttribute('aria-pressed', String(sound));
    $('audio').textContent = sound ? 'Вимкнути звук' : 'Увімкнути звук';
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

initChart();
void pollState();
setInterval(pollTicker, 3000);
setInterval(pollState, 30_000);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) { void pollState(); void pollTicker(); }
});
