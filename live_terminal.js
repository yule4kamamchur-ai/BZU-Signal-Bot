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
let selectedZone = null;
const reactions=new Map(), m5Proofs=new Map();
const invalidZones=new Set(), exhaustedPlans=new Set(), selectedPlanIds=new Map();
let m5Market=null, previousLiveSample=null, velocity=null, m5Busy=false;
let stateError = false, stateBusy = false, tickerBusy = false, candlesBusy = false;

const number = n => typeof n === 'number' && Number.isFinite(n);
const stamp = n => number(n) && n > 0 ? time.format(n) : '—';
const format = n => number(n) ? n.toFixed(Math.max(2, (String(state?.tick ?? .01).split('.')[1] ?? '').length)) : '—';
const positiveNumber=v=>(typeof v==='number'||typeof v==='string'&&v.trim()!=='')&&v!==null&&Number.isFinite(Number(v))&&Number(v)>0?Number(v):NaN;
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
  if (![1,-1].includes(order.side)||!number(order.entry)||order.entry<=0||!number(order.stop)||order.stop<=0||!number(order.risk)||order.risk<=0) return;
  const pending = order.status === 'PENDING';
  const open = order.status === 'OPEN';
  if (open && tracker.observedStatus !== 'OPEN') {
    // The new ledger resolves the pending order. Discard prior provisional exits.
    tracker.end = null;
    tracker.tp1 = false;
    tracker.quoteMfe = 0;
    tracker.quoteMae = 0;
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
  if (open && market.ts > state.snapshot_ts) {
    tracker.quoteMfe = Math.max(tracker.quoteMfe ?? 0, move / order.risk);
    tracker.quoteMae = Math.max(tracker.quoteMae ?? 0, -move / order.risk);
  }
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
      (full ? ' · рівень повної фіксації' : ' · очікуємо підтвердження часткової фіксації'));
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
  return number(row.score) ? formatScore(row.score) + '/100' + (row.grade ? ' ' + row.grade : '') : '—';
}
const formatScore = n => Number(n.toFixed(1)).toString();
function statusText(row, price) {
  if (row.actionable === true && validCandidate(row)) {
    const stage = livePlanState(row, price);
    if (stage === 'EXHAUSTED') return 'Перша ціль уже пройдена';
    if (stage === 'INVALID') return 'SL ПРОБИТО · ЗОНУ СПИСАНО';
    if (stage === 'MISSED') return 'Вікно реакції минуло';
    if (stage === 'EXPIRED') return 'Строк дії плану минув';
  }
  if (row.status === 'OPEN') return 'Позиція відкрита';
  if (row.status === 'PENDING') return 'Лімітка виставлена';
  if (row.status === 'CANCELLING') return 'Скасовуємо лімітку';
  if (actionablePlan(row, price)) {
    if (marketCandidate(row)) return 'Підтверджено сигнал MARKET';
    return row.tactic === 'REACTION_M5' ? 'Чекаємо реакції M5' : 'Чекаємо підходу до входу';
  }
  const reasons = {
    EXHAUSTED_MOVE: 'Перша ціль уже пройдена',
    MACRO_STOP_ANCHOR_REQUIRED: 'Бракує підтвердженої опори 1H/4H для стопа',
    MACRO_REVERSAL_REQUIRED: 'Для розвороту потрібен відбій від макроекстремуму',
    STRUCTURAL_STOP_TOO_WIDE: 'Структурний стоп виходить за допустиму межу',
    LEGACY_MICRO_RISK: 'Скасовуємо стару лімітку перед новим розрахунком',
    OIL_VOLATILITY_UNAVAILABLE: 'Бракує закритих свічок для розрахунку ATR',
    ORGANIC_TARGET_RANGE: 'Цілі поза діапазоном свінгового плану',
    TREND_M5_SPIKE: 'Пауза після аномальної свічки M5 проти напряму',
    SCORE_BELOW_THRESHOLD: 'Бал нижче порога для цього типу входу',
    COUNTER_CHOCH_REQUIRED: 'Потрібен підтверджений CHoCH на M15 або 1H',
    COUNTER_M5_REQUIRED: 'Після CHoCH потрібне підтвердження M5',
    ENTRY_TOO_CLOSE: 'Ціна вже надто близько до рівня для нового ліміту',
    STRUCTURAL_TP1_REQUIRED: 'Бракує структурної цілі для часткової фіксації',
    STRUCTURAL_TP2_REQUIRED: 'Бракує наступної структурної цілі',
    INSUFFICIENT_ROOM: number(row.target_barrier) ?
      'Перешкода біля ' + format(row.target_barrier) + ' обмежує шлях до цілі' :
      'Відстані до цілей поки не покривають потрібне співвідношення до ризику',
    RISK_TOO_WIDE: 'Структурний стоп потребує ' + format(row.risk) + ' $, ліміт ' + format(row.risk_cap) + ' $',
    RISK_BELOW_FLOOR: 'Стоп вужчий за мінімум ' + format(row.risk_floor) + ' $',
    STOP_OUTSIDE_RISK_BOUNDS: 'Структурний стоп поза межами ризику',
    RANGE_ZONE_TOO_CLOSE: 'У діапазоні рівень надто близько до ціни',
    NOT_IN_FINAL_REACTION_PLANS: 'Чекаємо підтвердження умов допуску',
    SCORE_BELOW_60: 'Сетап поки не набрав 60 балів',
    BLOCKED_BY_TREND: 'Зона проти поточного тренду',
    DISTANT_ZONE: 'Зона поки далеко від ціни',
    ZONE_NOT_AHEAD_OF_PRICE: 'Ціна вже пройшла точку входу',
    EXPIRED: 'Строк дії плану минув',
    STOP_ALREADY_BREACHED: 'Ціна вже пробила зону',
    IMPULSE_CONFIRMATION_REQUIRED: 'Чекаємо підтвердження після імпульсу'
  };
  return reasons[row.gate_reason] ?? 'Спостерігаємо за зоною';
}
// Independent validation at the public-data boundary. A chart label cannot authorize a trade.
function organicPlan(row) {
  return row.organic_risk?.model === 'ORGANIC_OIL_V32';
}
function targetRatiosValid(row, rr1, rr2) {
  return organicPlan(row) ?
    rr1 >= 1.40 - 1e-8 && rr1 <= 1.45 + 1e-8 && rr2 >= 1.8 - 1e-8 && rr2 <= 2.25 + 1e-8 :
    rr1 >= 1.4 - 1e-8 && rr2 >= 2.2 - 1e-8;
}
function marketCandidate(row) {
  return row.order_type === 'MARKET' &&
    (['M15_LEVEL_REJECTION','M15_BREAKDOWN_MOMENTUM'].includes(row.market_trigger?.pattern) || (row.trend_rel === 'WITH_TREND' && ['IN_ZONE_TOUCH','POST_TOUCH_REJECTION','BREAKOUT_CONTINUATION'].includes(row.market_trigger?.pattern)) ||
     (row.trend_rel === 'CONFIRMED_REVERSAL' && row.market_trigger?.pattern === 'CONFIRMED_REVERSAL'));
}
function validCandidate(row) {
  if (!row || ![1,-1].includes(row.side) || !['15m','1H','4H'].includes(row.timeframe)) return false;
  const locked={DEMAND_OB:1,FVG_BULL:1,BULLISH_BREAKER:1,BULL_BREAKER:1,BREAKER_BULL:1,SWEEP_LOW:1,IFVG_BULL:1,
    SUPPLY_OB:-1,FVG_BEAR:-1,BEARISH_BREAKER:-1,BEAR_BREAKER:-1,BREAKER_BEAR:-1,SWEEP_HIGH:-1,IFVG_BEAR:-1};
  const neutral=['CONFIRMED_FVG','BREAKER_FLIP_15M','MOMENTUM_STAIRCASE_15M','DISPLACEMENT_CATCHER_15M',
    'LIQUIDITY_RAID_ENGULFING_15M','STRUCTURAL_LEVEL','SESSION_SWEEP_ENGULFING',
    'SESSION_SWEEP_ENGULFING_15M','SWEEP_RECLAIM_ZONE','SESSION_OB','IMPULSE_OTE_15M',
    'SWEEP_REVERSAL','M15_CHOCH','BREAKER_CHOCH','MULTIBAR_SWEEP','RECLAIM_BREAKER','SR_FLIP_RECLAIM','BREAKDOWN_RECLAIM','CONTINUATION_PULLBACK','M5_IMPULSE_OB','FLAG_RETEST',
    'BREAKER_BLOCK','FVG_PULLBACK','FIB_OTE_ZONE','SWEEP_CONTINUATION','BOS_CONTINUATION'];
  if (locked[row.kind] ? locked[row.kind]!==row.side : !neutral.includes(row.kind)) return false;
  if (!['low','high','entry','stop','tp1','tp2'].every(k=>number(row[k])&&row[k]>0)||!number(row.score)||row.score<0) return false;
  const risk=row.side*(row.entry-row.stop);
  const proximal=row.side===1?row.high:row.low;
  const outside=row.entry<row.low || row.entry>row.high;
  const withinZone = marketCandidate(row) ?
    (['M15_LEVEL_REJECTION','M15_BREAKDOWN_MOMENTUM'].includes(row.market_trigger.pattern) ?
      Math.abs(row.entry - row.market_trigger.limit_entry) <= .20*row.market_trigger.initial_risk + 1e-8 :
      row.market_trigger.pattern === 'CONFIRMED_REVERSAL' ? !outside :
      row.market_trigger.pattern === 'IN_ZONE_TOUCH' ?
      row.entry >= row.low - .08000001 && row.entry <= row.high + .08000001 :
      row.side * (row.entry - proximal) >= 0 && row.side * (row.entry - proximal) <= .45 * risk + 1e-8) :
    (!outside || (row.side * (row.entry - proximal) >= 0 &&
      row.side * (row.entry - proximal) <= .03000001 + (state.tick || .01)));
  return row.low < row.high && risk > 0 &&
    targetRatiosValid(row, row.side * (row.tp1 - row.entry) / risk, row.side * (row.tp2 - row.entry) / risk) &&
    row.side * (row.tp2 - row.tp1) > 0 && row.score >= (row.min_score ?? 60) && row.score <= 100 &&
    number(row.known_ts) && row.known_ts <= state.snapshot_ts &&
    withinZone;
}
function maximumDistance(row) {
  if (number(row?.entry_max_distance) && row.entry_max_distance > 0) return row.entry_max_distance;
  return state.candidate_rules?.max_distance ?? Math.min(.65, 1.2 * (state.atr15 ?? .55));
}
function inEntryWindow(row, price) {
  const distance = row.side * (price - row.entry);
  const minimum = row.trend_rel === 'WITH_TREND' ? .08 : state.candidate_rules?.min_distance ?? .10;
  return distance >= minimum - 1e-8 && distance <= maximumDistance(row) + 1e-8;
}
function exhaustionKey(row) {
  return row.status === 'PENDING' || row.status === 'CANCELLING' ? 'order:' + (row.order_id ?? row.id) :
    zoneFingerprint(row);
}
function targetExhausted(row, price) {
  return row.gate_reason === 'EXHAUSTED_MOVE' || exhaustedPlans.has(exhaustionKey(row)) ||
    (number(row.tp1) && number(price) && row.side * (price - row.tp1) >= -1e-8);
}
function followsBias(row) {
  return !state.primary_bias || row.side === state.primary_bias ||
    row.reversal_override === true;
}
function actionablePlan(row, price, tracking = false) {
  if (row.actionable !== true || row.execution_admitted !== true || !['A','B+'].includes(row.grade) ||
      row.status !== 'CANDIDATE' || row.gate_reason || !['LIMIT','REACTION_M5','MARKET'].includes(row.tactic) ||
      !followsBias(row) || !validCandidate(row) ||
      (state.organic_risk_profile?.model === 'ORGANIC_OIL_V32' && !organicPlan(row))) return false;
  const stage = livePlanState(row, price);
  if (marketCandidate(row)) {
    const risk = row.side * (row.entry - row.stop);
    const proximal = row.side === 1 ? row.high : row.low;
    const tolerance = row.market_trigger.pattern === 'CONFIRMED_REVERSAL' ? 1e-8 : .08000001;
    const nearZone = price >= row.low - tolerance && price <= row.high + tolerance;
    const distance = row.side * (price - proximal);
    return !['INVALID','EXPIRED','MISSED','EXHAUSTED'].includes(stage) &&
      (nearZone || (!['IN_ZONE_TOUCH','CONFIRMED_REVERSAL'].includes(row.market_trigger.pattern) && distance > 0 && distance <= .45 * risk + 1e-8));
  }
  return !['INVALID','EXPIRED','MISSED','EXHAUSTED'].includes(stage) &&
    (inEntryWindow(row, price) || stage === 'READY' ||
      (tracking && Math.abs(price - row.entry) <= maximumDistance(row) + 1e-8));
}
function poolRows() { return state.plan_pool??state.candidates??[]; }
function zoneFingerprint(row) {
  return [state.instrument,row.kind,row.timeframe,row.side,row.source_low??row.low,row.source_high??row.high,row.known_ts].join('|');
}
function livePlanState(row,price) {
  if (targetExhausted(row, price)) return 'EXHAUSTED';
  if (invalidZones.has(zoneFingerprint(row)) || row.side*(price-row.stop)<=0) return 'INVALID';
  if (number(row.valid_until)&&row.valid_until<=Date.now()) return 'EXPIRED';
  const tracker=reactions.get(reactionKey(row));
  if (tracker?.stage==='MISSED') return 'MISSED';
  if (tracker?.stage === 'READY') {
    const now = Date.now();
    const expired = now - tracker.readyTs > (state.reaction_config?.ready_ttl_ms ?? 15000) ||
      Math.floor(now / 300000) !== tracker.bucket;
    return expired ? 'MISSED' : 'READY';
  }
  if (tracker?.stage==='TOUCHED') return 'TOUCHED';
  if (row.low<=price&&price<=row.high) return 'IN_ZONE';
  return row.side*(price-row.entry)>0?'APPROACH':'WAIT_REACTION';
}
function radarRows(price = displayedPrice()) {
  if (!ready(Date.now())) return [null, null];
  const bias = state.primary_bias || 0;
  const rows = [1, -1].map(side => {
    const choices = poolRows().filter(row => row.side === side && actionablePlan(row, price));
    // A witnessed touch owns the live check until resolved; do not chase another zone mid-rejection.
    const held=choices.find(row=>selectedPlanIds.get(side)===row.id&&['TOUCHED','READY'].includes(livePlanState(row,price)));
    choices.sort((a,b) => Math.abs(a.entry - price) - Math.abs(b.entry - price) ||
      b.score - a.score || b.rr1 - a.rr1);
    const selected=held??choices[0]??null;
    if (selected) selectedPlanIds.set(side,selected.id); else selectedPlanIds.delete(side);
    return selected;
  });
  if (rows[0]&&rows[1] && (rows[1].entry-rows[0].entry < Math.max(.65, state.min_structural_spread || 0,
    state.organic_risk_profile ? .8 * Math.max(rows[0].risk ?? 0, rows[1].risk ?? 0) : 0) || rows[0].high>=rows[1].low)) {
    const keep=bias || (rows[0].score>=rows[1].score?1:-1);rows[keep===1?1:0]=null;
  }
  return rows;
}
function mainPlan(rows=radarRows().filter(Boolean)) {
  return rows.find(r=>r.id===state.primary_candidate_id)||rows.find(r=>r.side===state.primary_bias)||[...rows].sort((a,b)=>b.score-a.score)[0]||null;
}
function reactionRows() {
  if (state.reaction_config?.execution_confirmation === 'CLOSED_M15_REJECTION') return [];
  if (![4,5].includes(state.cockpit_schema) || state.orders.some(o=>o.status==='OPEN') || !ready(Date.now())) return [];
  const price = displayedPrice();
  const chosen = radarRows(price).filter(r => r?.reaction_eligible);
  let rows = [1, -1].map(side => chosen.find(r => r.side === side) ??
    poolRows().filter(r => r.side === side && r.reaction_eligible && actionablePlan(r, price, true))
      .sort((a,b) => Math.abs(a.entry - price) - Math.abs(b.entry - price))[0]).filter(Boolean);
  for (const order of state.orders) {
    if (order.status!=='PENDING' || order.trend_rel === 'WITH_TREND' || order.expires_ts<=Date.now()) continue;
    const row={...order,...order.zone,id:'order:'+order.id,score_source:'CONTEXT',reaction_eligible:true,valid_until:order.expires_ts};
    if (validCandidate(row) && pendingIsCurrent(order, price)) {
      rows=rows.filter(r=>!(r.side===row.side && r.entry===row.entry && r.stop===row.stop && r.low===row.low && r.high===row.high));
      rows.unshift(row);
    }
  }
  return rows;
}
function reactionKey(row) {
  return [state.instrument,row.id,row.kind,row.side,row.source_low??row.low,row.source_high??row.high,row.known_ts,
    row.entry,row.stop,row.tp1,row.tp2].join('|');
}
function pauseReactions() {
  previousLiveSample=null;
  for (const t of reactions.values()) if (!['INVALID','MISSED'].includes(t.stage)) {
    t.stage='UNARMED'; t.touchTs=null; t.peak=null; t.samples=0;
  }
}
function reactionTracker(row) {
  const key=reactionKey(row);
  if (!reactions.has(key)) reactions.set(key,{stage:'UNARMED',samples:0,lastTs:0,touchTs:null,peak:null,
    bucket:null,readyTs:null,events:new Set(),message:'Потрібен свіжий підхід до зони.'});
  return reactions.get(key);
}
function updateReaction(row, market) {
  const t=reactionTracker(row), cfg=state.reaction_config??{}, tick=state.tick||.01;
  if (market.ts<=t.lastTs || ['INVALID','MISSED'].includes(t.stage)) return t;
  const bucket=Math.floor(market.ts/300000), gap=market.ts-t.lastTs;
  const reset=t.lastTs && (gap>(cfg.max_sample_gap_ms??15000)||t.bucket!==bucket);
  if (reset && t.stage==='READY') {
    t.stage='MISSED';t.message='Змінилася свічка M5 або перервався потік. Попередня реакція втратила актуальність.';
    t.bucket=bucket;t.lastTs=market.ts;return t;
  }
  if (reset && t.stage!=='READY') {t.stage='UNARMED';t.touchTs=null;t.peak=null;t.samples=0;}
  t.bucket=bucket;t.lastTs=market.ts;t.samples++;
  const price=market.price, risk=row.side*(row.entry-row.stop), proximal=row.side===1?row.high:row.low;
  const remainingRisk=row.side*(price-row.stop);
  const depth=row.side===1?(row.high-price)/(row.high-row.low):(price-row.low)/(row.high-row.low);
  if (remainingRisk<=0) {
    invalidZones.add(zoneFingerprint(row));
    t.stage='INVALID';t.message='Ціна пробила SL. Зону списано локально; радар обирає наступний рівень.';
    return t;
  }
  if (t.stage==='READY') {
    if (market.ts-t.readyTs>(cfg.ready_ttl_ms??15000) || row.side*(price-row.entry)>(cfg.max_chase_r??.25)*risk ||
        row.side*(price-t.readyPrice)<-tick || (state.cockpit_schema === 5 && !m5QuotePlan(row, price))) {
      t.stage='MISSED';t.message='Реакція втратила актуальність. Не наздоганяємо ціну.';
    }
    return t;
  }
  if (state.cockpit_schema === 5) {
    t.stage = row.low <= price && price <= row.high ? 'TOUCHED' : 'ARMED';
    t.message = 'Чекаємо закриття M5 з відбоєм, поглинанням або ретестом зламу.';
    return t;
  }
  const outside=row.side*(price-proximal)>0;
  if (t.stage==='UNARMED') {
    if (outside) {t.stage='ARMED';t.message='Чекаємо спостережений дотик зони.';}
    else t.message='Вкладку відкрито вже у зоні. Потрібен новий підхід; минулий дотик не підтверджено.';
    return t;
  }
  const inside=row.low<=price&&price<=row.high;
  if (t.stage==='ARMED'&&inside) {
    t.stage='TOUCHED';t.touchTs=market.ts;t.peak=price;t.touchSample=t.samples;
    t.message='Дотик зони спостережено. Чекаємо відбій, M5 ще формується.';
    return t;
  }
  if (t.stage==='TOUCHED') {
    t.peak=row.side===1?Math.min(t.peak,price):Math.max(t.peak,price);
    const rejection=row.side*(price-t.peak), minReturn=cfg.retracement??Math.max(.06,3*tick);
    const newRR=remainingRisk>0?row.side*(row.tp1-price)/remainingRisk:0;
    if (rejection>=minReturn-1e-8 && row.side*(price-row.entry)>=tick-1e-8 &&
        t.samples>t.touchSample && t.samples>=(cfg.min_samples??3)) {
      const quotePlan = organicPlan(row) ? m5QuotePlan(row, price) :
        row.side * (price - row.entry) <= (cfg.max_chase_r ?? .25) * risk && newRR >= 1.4 - 1e-8;
      if (!quotePlan) {
        t.stage='MISSED';t.message='Відбій спостерігався, але ціна вже погіршила R:R. Вхід пропущено.';
      } else if (t.events.has('REACTION')) {
        t.stage='MISSED';t.message='Цю реакцію вже показано. Повторний сигнал не створюється.';
      } else {
        t.stage='READY';t.readyTs=market.ts;t.readyPrice=price;t.rr=newRR;
        t.message='Локальний відбій за отриманими тиками. Закриття M5 ще не підтверджено.';
        notify(row,t,'REACTION','Live реакція '+sideLabel(row.side)+' @ '+format(price)+' · перевірте вхід');
      }
    }
  }
  return t;
}
function observeLive(market) {
  const now=Date.now();
  if (!ready(now)||document.hidden||now-market.ts>QUOTE_MAX_AGE||market.ts>now+3000||Math.floor(now/300000)!==Math.floor(market.ts/300000)) {pauseReactions();return;}
  if (previousLiveSample && market.ts<=previousLiveSample.ts) return;
  const gap=previousLiveSample?market.ts-previousLiveSample.ts:0;
  if (gap>15000) pauseReactions();
  velocity=previousLiveSample && market.ts>previousLiveSample.ts ?
    (market.price-previousLiveSample.price)/((market.ts-previousLiveSample.ts)/1000):null;
  previousLiveSample={...market};
  const bucket=Math.floor(market.ts/300000)*300;
  if (!m5Market||m5Market.time!==bucket) m5Market={time:bucket,open:market.price,high:market.price,low:market.price,
    close:market.price,source:'OBSERVED_TICKS',received:now};
  else {m5Market.high=Math.max(m5Market.high,market.price);m5Market.low=Math.min(m5Market.low,market.price);m5Market.close=market.price;}
  for (const row of poolRows()) if (row.actionable===true&&validCandidate(row)&&row.status==='CANDIDATE'&&row.side*(market.price-row.stop)<=0&&!invalidZones.has(zoneFingerprint(row))) {
    invalidZones.add(zoneFingerprint(row));const t=reactionTracker(row);t.stage='INVALID';
    notify(row,t,'ZONE_SL','SL плану '+format(row.stop)+' пробито · зону списано локально');
  }
  for (const row of [...poolRows().filter(row => !row.order_id), ...state.orders.filter(order => order.status === 'PENDING')]) {
    if (number(row.tp1) && row.side * (market.price - row.tp1) >= -1e-8) {
      exhaustedPlans.add(exhaustionKey(row));
    }
  }
  const keep=new Set();
  for (const row of reactionRows()) {keep.add(reactionKey(row));updateReaction(row,market);}
  for (const [key,t] of reactions) if (!keep.has(key)&&!['INVALID','MISSED'].includes(t.stage)) reactions.delete(key);
}
function currentReaction() {
  const rows=reactionRows();
  const primary=rows.find(r=>r.id.startsWith('order:')) || mainPlan(rows);
  return primary?{row:primary,tracker:reactionTracker(primary)}:null;
}
function reactionPresentation(usable, live) {
  if (!usable||!live||document.hidden) return {stage:'PAUSED',title:'Live реакція призупинена',detail:'Потрібні актуальний зліпок і свіжі котирування у відкритій вкладці.'};
  if (state.orders.some(o=>o.status==='OPEN')) return {stage:'PAUSED',title:'Супроводжуємо позицію',detail:'Пошук нового live входу призупинено, доки позиція відкрита.'};
  const current=currentReaction();
  if (!current) return {stage:'WAIT',title:'Чекаємо реакційний план від 60/100',detail:'Слабкі, неповні та протитрендові рівні залишаються довідковими.'};
  const {row,tracker:t}=current;
  const expired=t.stage==='READY' && (Date.now()-t.readyTs>(state.reaction_config?.ready_ttl_ms??15000)||Math.floor(Date.now()/300000)!==t.bucket);
  const stage=expired?'MISSED':t.stage;
  return {stage,row,tracker:t,title:({UNARMED:'Чекаємо свіжий підхід',ARMED:'План підготовлено · чекаємо дотик',
    TOUCHED:'У ЗОНІ · чекаємо реакцію M5',READY:state.cockpit_schema === 5 ? 'РЕАКЦІЮ M5 ПІДТВЕРДЖЕНО' : '🎯 СНАЙПЕРСЬКИЙ СИГНАЛ · LIVE РЕАКЦІЯ M5',INVALID:'ЛОКАЛЬНИЙ ВХІД ЗАБЛОКОВАНО',
    MISSED:'Вікно реакції минуло'})[stage],detail:expired?'Не наздоганяємо ціну після завершення вікна реакції.':t.message};
}
function renderReaction(usable,live) {
  const view=reactionPresentation(usable,live), panel=$('reaction-monitor');
  panel.dataset.stage=view.stage;
  $('reaction-title').textContent=view.title;
  $('reaction-detail').textContent=view.detail;
  const bar=m5Market&&Math.floor(Date.now()/300000)*300===m5Market.time?m5Market:null;
  $('reaction-metrics').textContent=live ? 'Швидкість '+(number(velocity)?signed(velocity,' $/с'):'накопичуємо тики')+
    (bar?' · тіло M5 '+format(Math.abs(bar.close-bar.open))+' $ · '+(bar.source==='OKX_M5'?'OKX M5':'часткові спостереження'):' · M5 ще не отримано') : 'Оцінюємо кожне отримане котирування; REST — раз на 3 с.';
  if (view.stage==='READY') $('reaction-detail').textContent+=' '+sideLabel(view.row.side)+' @ '+format(view.tracker.readyPrice)+
    ' | SL '+format(view.row.stop)+' | TP1 '+format(view.row.tp1)+' | TP2 '+format(view.row.tp2)+' | R:R '+view.tracker.rr.toFixed(2);
}
function closedM5Pattern(row, bars, tick) {
  const last = bars.at(-1), prev = bars.at(-2);
  if (!last || !prev || last.ts - prev.ts !== 300000 || last.ts < row.known_ts ||
      last.low > row.high || last.high < row.low ||
      row.side * (last.close - row.entry) < tick - 1e-8 ||
      row.side * ((row.side === 1 ? last.low : last.high) - row.stop) <= 0 ||
      row.side * (last.close - last.open) <= 0) return null;
  const side = row.side;
  const wick = side === 1 ? Math.min(last.open, last.close) - last.low :
    last.high - Math.max(last.open, last.close);
  if (wick >= Math.max(.06, 3*tick) - 1e-8 && wick >= .35 * (last.high - last.low)) return 'Відбій із тінню';
  if (side * (prev.close - prev.open) < 0 && side * (last.open - prev.close) <= tick &&
      side * (last.close - prev.open) >= tick - 1e-8) return 'Поглинання';
  const prior = bars.slice(-5,-2);
  const contiguous = prior.length === 3 && [prior[1],prior[2],prev].every((bar,i)=>bar.ts-prior[i].ts===300000);
  const level = row.m5_breaker?.known_ts <= last.ts ? row.m5_breaker.level :
    contiguous ? (side === 1 ? Math.max(...prior.map(bar=>bar.high)) : Math.min(...prior.map(bar=>bar.low))) : null;
  if (!number(level) || level < row.low || level > row.high) return null;
  const broke = row.m5_breaker?.known_ts <= last.ts ||
    (side * (prev.open - level) <= tick / 2 && side * (prev.close - level) >= tick - 1e-8);
  if (broke && last.low <= level + tick && last.high >= level - tick &&
      side * (last.close - level) >= tick - 1e-8) return 'Ретест зламу';
  return null;
}
function m5QuotePlan(row, price) {
  const settings = state.reaction_config ?? {};
  const risk = row.side * (price - row.stop);
  const rr = risk > 0 ? row.side * (row.tp1 - price) / risk : 0;
  const rr2 = risk > 0 ? row.side * (row.tp2 - price) / risk : 0;
  const costs = (price + row.tp2) * (settings.taker_fee ?? 0);
  const plannedRisk = row.side * (row.entry - row.stop);
  const move = row.side * (price - row.entry);
  const maxChase = (settings.max_chase_r ?? .25) * plannedRisk;
  const floor = organicPlan(row) ? Math.max(1.15, Math.round(price * 1.25) / 100) : row.risk_floor ?? .22;
  const cap = organicPlan(row) ? Math.max(2.10, Math.round(price * 2.2) / 100) : row.risk_cap ?? .65;
  if (move < 0 || move > maxChase || risk < floor - 1e-8 || risk > cap + 1e-8 ||
      !targetRatiosValid(row, rr, rr2) ||
      (organicPlan(row) ? rr <= (price + row.tp1) * (settings.taker_fee ?? 0) / risk :
        rr2 - costs / risk < (settings.min_net_rr ?? 1.8) - 1e-8)) return null;
  return {rr};
}
function observeClosedM5(bars) {
  const now = Date.now(), known = bars.at(-1)?.ts + 300000;
  if (state.cockpit_schema !== 5 || document.hidden || !ready(now) || !quote ||
      !number(known) || known > now || now - known > 60_000 ||
      quote.ts < known || quote.ts > now + 3000 || now - quote.ts > QUOTE_MAX_AGE) return;
  for (const [key, ts] of m5Proofs) if (now - ts > 60_000) m5Proofs.delete(key);
  for (const row of reactionRows()) {
    if (row.id.startsWith('order:')) continue;
    const tracker = reactionTracker(row);
    if (['READY','MISSED','INVALID'].includes(tracker.stage)) continue;
    const pattern = closedM5Pattern(row, bars, state.tick || .01);
    const fingerprint = zoneFingerprint(row);
    if (!pattern || m5Proofs.get(fingerprint) === known) continue;
    const geometry = m5QuotePlan(row, quote.price);
    if (!geometry) continue;
    Object.assign(tracker, {stage:'READY', readyTs:quote.ts, readyPrice:quote.price, rr:geometry.rr,
      lastTs:quote.ts, bucket:Math.floor(quote.ts/300000),
      message:pattern + ' на закритій M5. Рівень за свіжою ціною пройшов перевірку ризику.'});
    m5Proofs.set(fingerprint, known);
    notify(row, tracker, 'M5:' + known, pattern + ' M5 · ' + sideLabel(row.side) + ' @ ' + format(quote.price));
  }
}
async function pollM5() {
  if (!state || m5Busy || document.hidden) return;
  m5Busy = true;
  const instrument = state.instrument;
  try {
    const body = await json(OKX + '/candles?' + new URLSearchParams({instId:instrument,bar:'5m',limit:'8'}));
    if (body.code !== '0' || state.instrument !== instrument) return;
    const now = Date.now(), bucket = Math.floor(now / 300000) * 300000;
    const closed = [];
    for (const raw of body.data ?? []) {
      const [ts,open,high,low,close] = raw.slice(0,5).map(Number);
      if (![ts,open,high,low,close].every(number) || ts % 300000 !== 0 ||
          Math.min(open,high,low,close) <= 0 || low > Math.min(open,close) || high < Math.max(open,close)) continue;
      if (ts === bucket) m5Market = {time:ts/1000,open,high,low,close,source:'OKX_M5',received:now};
      if (String(raw[8]) === '1' && ts + 300000 <= now) closed.push({ts,open,high,low,close});
    }
    observeClosedM5(closed.sort((a,b)=>a.ts-b.ts));
  } catch { /* Tick observations remain explicitly partial. */ }
  finally { m5Busy = false; }
}

function zoneName(kind) {
  if (typeof kind !== 'string') return 'Структурна зона';
  if (kind.includes('OTE')) return 'Відкат імпульсу OTE';
  if (kind?.includes('BREAKER')) return 'Ретест зламаного рівня';
  if (kind?.includes('FVG')) return 'Імбаланс';
  if (kind?.includes('SWEEP') || kind?.includes('RAID')) return 'Хибний пробій';
  return ({DEMAND_OB:'Зона попиту', SUPPLY_OB:'Зона пропозиції',
           DISPLACEMENT_CATCHER_15M:'Відкат після імпульсу',
           SWEEP_REVERSAL:'Зняття ліквідності й розворот',
           M15_CHOCH:'Розворот структури M15', BREAKER_CHOCH:'Розворот структури M5',
           BREAKER_BLOCK:'Дзеркальний рівень', BOS_CONTINUATION:'Мілкий ретест пробою', FVG_PULLBACK:'Відкат у FVG',
           FIB_OTE_ZONE:'Відкат 50–61.8%', SWEEP_CONTINUATION:'Зняття ліквідності за трендом',
           MULTIBAR_SWEEP:'Свіп із поглинанням', BREAKDOWN_RECLAIM:'Ведмежий пробій підтримки', SR_FLIP_RECLAIM:'Дзеркальний рівень', RECLAIM_BREAKER:'Ретест повернутого рівня',
           CONTINUATION_PULLBACK:'Відкат поточної хвилі', M5_IMPULSE_OB:'Імпульсна зона M5',
           FLAG_RETEST:'Ретест пробою прапора', BEARISH_BREAKER:'Дзеркальний опір',
           BULLISH_BREAKER:'Дзеркальна підтримка', MOMENTUM_STAIRCASE_15M:'Продовження тренду'})[kind] ?? 'Структурна зона';
}
function watchable(row, price) {
  return row && ['entry','stop','tp1','tp2','risk','rr1','rr2','score','low','high'].every(key=>number(row[key])) &&
    !row.order_id && !targetExhausted(row, price) && !invalidZones.has(zoneFingerprint(row)) && row.risk_gate !== 'STOP_ALREADY_BREACHED' &&
    row.side * (price - (row.side === 1 ? row.low : row.high)) >= 0 &&
    row.side * (price - row.stop) > 0;
}
function reserveLevel(side, price, leader) {
  if (state.cockpit_schema !== 5 || (state.primary_bias && side !== state.primary_bias)) return null;
  return poolRows().filter(row => row.side === side && watchable(row, price) &&
    side * (price - row.entry) > maximumDistance(row) + 1e-8 &&
    (!state.organic_risk_profile || !leader || Math.abs(row.entry - leader.entry) >=
      Math.max(1, .8 * Math.max(row.risk, leader.risk)) - 1e-8))
    .sort((a,b) => Math.abs(a.entry - price) - Math.abs(b.entry - price) || b.score - a.score)[0] ?? null;
}
function organicStopText(row) {
  if (!organicPlan(row)) return '';
  const profile = row.organic_risk;
  const stop = row.initial_stop ?? row.stop;
  const risk = row.side * (row.entry - stop);
  if (!(risk > 0)) return '';
  const protection = profile.atr_1h > 0 ?
    (risk / profile.atr_1h).toFixed(2) + '× ATR ' + (profile.atr_source === '15M_X_2_15' ? '1H (оцінка)' : '1H') : '';
  const anchor = profile.macro_anchor;
  return 'Органічний стоп ' + format(stop) + ' (' + signed(stop - row.entry, ' $') +
    ' / ' + (risk / row.entry * 100).toFixed(2) + '%)' +
    (protection ? ' · ' + protection : '') +
    (number(profile.adr_d1) ? ' · ADR ' + format(profile.adr_d1) + ' $ за ' + profile.adr_days + ' дн.' : '') +
    (number(profile.session_multiplier) ? ' · множник сесії ' + format(profile.session_multiplier) : '') +
    (anchor ? ' · за рівнем ' + anchor.timeframe + ' ' + format(anchor.level) : '');
}
function planText(row) {
  const source1 = row.tp1_source === 'QUOTE_REFERENCE' ? ', ціна зліпка' :
    row.tp1_source?.startsWith('R_MULTIPLE') ? ', розрахунок' : '';
  const source2 = row.tp2_source === 'REFERENCE_2R' ? ', орієнтир' :
    row.tp2_source?.startsWith('R_MULTIPLE') ? ', розрахунок' : '';
  const risk = row.side * (row.entry - (row.initial_stop ?? row.stop));
  const target = (level, source) => risk > 0 && number(level) ?
    signed(level - row.entry, ' $') + ' / ' + (row.side * (level - row.entry) / risk).toFixed(2) + 'R' + source : '—';
  return 'Вхід ' + format(row.entry) + ' · ' +
    (organicStopText(row) || 'Стоп ' + format(row.stop) + ' (' + format(risk) + ' $)') + ' · ' +
    'Тейк 1 ' + format(row.tp1) + ' (' + target(row.tp1, source1) + ') · Тейк 2 ' +
    format(row.tp2) + ' (' + target(row.tp2, source2) + ')';
}
function conditionText(row, price) {
  if (!followsBias(row)) {
    return 'Для входу проти тренду потрібен підтверджений відбій від екстремуму 1H, 4H або попередньої доби.';
  }
  if (!ready(Date.now()) || !actionablePlan(row, price)) {
    return 'Орієнтир для спостереження. ' + statusText(row, price) +
      (row.low <= price && price <= row.high ? '. Для входу потрібні допуск і закрита реакція M5.' : '.');
  }
  if (marketCandidate(row)) {
    if (row.market_trigger.pattern === 'M15_BREAKDOWN_MOMENTUM') return 'Закрита M15 підтвердила імпульсний пробій підтримки.';
    return ['POST_TOUCH_REJECTION','M15_LEVEL_REJECTION'].includes(row.market_trigger.pattern) ? 'Зафіксовано відбій від зони.' :
      row.market_trigger.pattern === 'BREAKOUT_CONTINUATION' ? 'Підтверджено імпульсний пробій.' : 'Ціна в межах допуску зони.';
  }
  if (row.low <= price && price <= row.high) return 'Ціна в зоні. Чекаємо відбою на закритій M5.';
  if (row.entry_condition === 'M5_BREAKER_RETEST') return 'Чекаємо ретесту зламаного рівня на закритій M5.';
  return row.tactic === 'REACTION_M5' ? 'Чекаємо підходу та відбою на закритій M5.' : 'Чекаємо підходу до рівня лімітки.';
}

function pendingIsCurrent(order, price) {
  return number(order.entry) && !targetExhausted(order, price) &&
    Math.abs(price - order.entry) <= maximumDistance(order) + 1e-8;
}
function chartOrders() {
  const price = displayedPrice();
  return state.orders.filter(order => order.status === 'OPEN' ||
    (order.status === 'PENDING' && pendingIsCurrent(order, price) &&
      (!number(order.expires_ts) || order.expires_ts > Date.now())));
}
function chartPlan() {
  if (state.orders.length) return null;
  return mainPlan();
}
function visibleZones() {
  const orders = chartOrders();
  if (orders.length) return state.zones.filter(zone => orders.some(order => order.id === zone.order_id));
  const primary = chartPlan();
  if (primary) return [primary];
  const price = displayedPrice();
  return (state.context_bands ?? []).filter(zone => !invalidZones.has(zoneFingerprint(zone)) &&
    !targetExhausted(poolRows().find(row => row.id === zone.id) ?? zone, price) &&
    Math.max(zone.low - price, price - zone.high, 0) <= maximumDistance() &&
    zone.side * (price - (zone.side === 1 ? zone.low : zone.high)) > 0).slice(0, 2);
}
function displayedPrice() {
  const now = Date.now();
  return quote && quote.ts <= now + 3000 && now - quote.ts <= QUOTE_MAX_AGE ? quote.price : state.price;
}
function renderZones(price) {
  const container = $('zones');
  container.replaceChildren();
  $('zone-count').textContent = state.zones.length + ' зон';
  const focusable = chartPlan()?.id;
  const reserves = new Set(Object.values(state.reserve_levels ?? {}));
  for (const z of state.zones) {
    const row = element('tr');
    if (z.id === selectedZone) row.classList.add('selected');
    const name = td('');
    name.append(element('strong', (z.timeframe ?? '') + ' ' + zoneName(z.kind)),
      element('small', sideLabel(z.side) + ' · ' + format(z.low) + '–' + format(z.high)));
    const approved = ready(Date.now()) && actionablePlan(z, price);
    const score = td(scoreText(z));
    const counter = !followsBias(z);
    const inside = z.low <= price && price <= z.high;
    const status = targetExhausted(z, price) ? 'Відпрацьовано · ціль пройдена' : counter ? (z.side === -1 ? 'Опір проти тренду' : 'Підтримка проти тренду') :
      inside ? (approved ? 'Ціна в зоні · чекаємо M5' : 'Ціна в зоні · спостерігаємо') : z.id === focusable ?
      (approved ? 'Головна ціль' : 'Головний орієнтир') : reserves.has(z.id) ? 'Резерв по тренду' : statusText(z, price);
    const entry = td(status);
    if (state.cockpit_schema === 5 && number(z.rr1) && number(z.rr2)) {
      entry.append(element('small', planText(z)));
      if (status !== statusText(z, price)) entry.append(element('small', statusText(z, price)));
    } else if (approved) {
      entry.append(element('small', 'Вхід ' + format(z.entry) + ' · SL ' + format(z.stop) +
        ' · TP1 ' + format(z.tp1) + ' · TP2 ' + format(z.tp2)));
    }
    const action = td('');
    if (z.id === focusable) {
      const focus = element('button', '◎');
      focus.type = 'button'; focus.dataset.zone = z.id;
      focus.dataset.focus = 'zone-' + z.id;
      focus.setAttribute('aria-label', 'Підсвітити ' + zoneName(z.kind) + ' ' + (z.timeframe ?? '') + ' ' + sideLabel(z.side));
      focus.setAttribute('aria-pressed', String(z.id === selectedZone));
      action.append(focus);
    }
    row.append(name, score, entry, action); container.append(row);
  }
  if (!container.children.length) {
    const row = element('tr'), cell = td('Сканер ще не передав зон. Чекаємо наступний цикл.');
    cell.colSpan = 4; row.append(cell); container.append(row);
  }
}
function copyButtons(row) {
  const wrap = element('div', null, 'copy-actions');
  for (const [key,label] of [['entry','Entry'], ['stop','SL'], ['tp1','TP1'], ['be_stop','Б/У']]) {
    if (!number(row[key]) || (key === 'be_stop' && (!row.tp1_partial || !number(row.be_confirmed_ts)))) continue;
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
    ', TP1 ' + format(order.tp1) + ', TP2 ' + format(order.tp2) +
    ', поточна ціна ' + format(price) + '. Позначки етапів у рівних інтервалах.');
  track.append(element('div', null, 'lifecycle-track'));
  const milestones = [['SL',order.stop],['Вхід',order.entry],['TP1',order.tp1],['TP2',order.tp2]]
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
    const confirmed = group.labels.includes('TP1') ? order.tp1_partial : group.labels.includes('Вхід');
    if (confirmed) dot.classList.add('reached');
    dot.append(element('span', group.labels.join(' / ')), element('span', format(group.value)));
    track.append(dot);
  }
  const live = element('div', format(price), 'live-marker');
  live.style.left = Math.max(5,Math.min(95,marker)) + '%'; track.append(live);
  wrap.append(track,element('p','Етапи у рівних інтервалах · ціна рухається між рівнями','muted micro'));
  return wrap;
}
function tradeDuration(ms) {
  const mins = Math.floor(Math.max(0, ms) / 60000);
  return Math.floor(mins / 60) + ' год ' + mins % 60 + ' хв';
}
const deadlineTime = new Intl.DateTimeFormat('uk-UA', {timeZone:'Europe/Kyiv', hour:'2-digit', minute:'2-digit'});
function tradeDeadline(node, now) {
  const entered = Number(node.dataset.tradeEntered);
  const deadline = Number(node.dataset.tradeDeadline);
  const elapsed = now - entered;
  const hasDeadline = deadline > 0;
  const remaining = deadline - now;
  node.textContent = tradeDuration(elapsed);
  if (hasDeadline) {
    const cutoff = deadlineTime.format(deadline);
    node.textContent += remaining > 0 ?
      ' · до ' + cutoff + ' · ще ' + tradeDuration(remaining) :
      ' · ' + cutoff + ' — ВИЧЕРПАНО на ' + tradeDuration(-remaining);
  }
  node.classList.toggle('trade-time-overdue', hasDeadline && remaining <= 0);
  node.classList.toggle('trade-time-warning', hasDeadline && remaining > 0 && elapsed >= 4 * 3600000);
}
function positionMetrics(details, order, tracker, now) {
  const info = order.position_guard;
  if (!info || !number(order.filled_ts)) return;
  const clock = element('dd');
  clock.dataset.tradeEntered = order.filled_ts;
  clock.dataset.tradeDeadline = info.mode === 'POST_TP1' ? 0 : (info.stale_at ?? 0);
  tradeDeadline(clock, now);
  const mfe = Math.max(order.max_favorable_r ?? 0, tracker.quoteMfe ?? 0);
  const mae = Math.max(order.max_adverse_r ?? 0, tracker.quoteMae ?? 0);
  const quoteOnly = mfe > (order.max_favorable_r ?? 0) || mae > (order.max_adverse_r ?? 0);
  const peak = order.entry + order.side * mfe * order.risk;
  const regime = ({BULLISH_ACTIVE:'Висхідний', BULLISH_TREND:'Висхідний', BEARISH_ACTIVE:'Низхідний',
    BEARISH_TREND:'Низхідний', PULLBACK_PHASE:'Відкат', RANGE:'Діапазон', RANGE_LIQUIDITY_ROTATION:'Діапазон'})[info.regime];
  const structure = ({ADVERSE:'⚠️ Зустрічний злам', COUNTER_TREND:'⚠️ Контртренд', WEAKENING:'⚠️ Послаблення',
    REVERSAL_RISK:'⚠️ Ризик розвороту', INTACT:'Без підтвердженого зламу', INSUFFICIENT:'Бракує свіжих даних'})[info.structure] ?? 'Невизначено';
  details.append(element('dt','Час в угоді / Ліміт'), clock,
    element('dt','Пік прибутку (MFE)'), element('dd', signed(mfe) + 'R (' + format(peak) + ') · MAE ' +
      signed(-mae) + 'R' + (quoteOnly ? ' · оцінка за тиками' : '')),
    element('dt','Статус структури'), element('dd', structure + (regime ? ' (' + regime + ')' : '')));
  const labels = {ROUND_TRIP:['ROUND-TRIP GUARD ACTIVE','guard'], ROUND_TRIP_TIMER:['ROUND-TRIP · З ' + stamp(info.round_trip_at),'guard'],
    STALE_WARNING:['STALE TRADE WARNING','warning'], STALE_TIMEOUT:['ТАЙМ-СТОП · ПЕРЕВІРКА','warning'],
    STALE_TIGHTENED:['ТАЙМ-СТОП · SL ' + format(info.stale_stop),'warning'], ADVERSE_CHOCH:['СТРУКТУРА ПРОТИ ПОЗИЦІЇ','warning']};
  const badge = labels[info.mode];
  if (badge) details.append(element('dt','Захист'), element('dd',badge[0],'protection-badge ' + badge[1]));
}

function management(order, tracker, price, usable) {
  if (!usable) return 'Останній стан за журналом. Оновіть дані перед перевіркою позиції.';
  if (order.status==='CANCELLING') return 'Бот запросив скасування. Перевірте ордер і можливе виконання у брокера.';
  if (tracker.end) return ({ SL:'Ціна торкнулася SL. Перевірте закриття у брокера.',
    TP1:'Ціна торкнулася цілі повного виходу. Перевірте закриття у брокера.',
    TP2:'Ціна торкнулася TP2. Перевірте закриття залишку у брокера.',
    EXPIRED:'Строк лімітки минув. Очікуємо підтвердження журналу.' })[tracker.end];
  if (order.status==='PENDING') {
    if (order.entry_hold === 'CONFIRMATION_REQUIRED') return 'Ліміт призупинено до підтвердження M15.';
    const watching=reactionRows().find(r=>r.id==='order:'+order.id);
    if (watching && reactions.get(reactionKey(watching))?.stage==='INVALID') return 'Локальний вхід заблоковано через пробій. Лімітка в журналі ще чинна; перевірте її у брокера.';
  }
  if (order.status==='PENDING') return tracker.touch ?
    'Ціна торкнулася Entry. Виконання ще не підтверджене журналом.' :
    'Чекаємо виконання лімітки. SL ' + format(order.stop) + ' · TP1 ' + format(order.tp1) + '.';
  if (order.tp1_partial) {
    if (number(order.pending_stop) && number(order.be_confirmed_ts)) {
      return 'TP1 зафіксовано, стоп Б/У ' + format(order.pending_stop) +
        ' набуде чинності о ' + stamp(order.pending_stop_effective_ts) + '.';
    }
    if (number(order.be_stop) && order.side * (order.stop - order.be_stop) >= -1e-8) {
      return 'Стоп уже захищає залишок до TP2 ' + format(order.tp2) + '.';
    }
    return order.be_waits_for_structure === false ?
      'Частину позиції зафіксовано, супроводжуємо залишок до TP2.' :
      'TP1 зафіксовано, чекаємо структуру M5 для перенесення стопа.';
  }
  if (tracker.tp1) return 'Ціна досягла TP1, чекаємо підтвердження фіксації.';
  if (order.position_guard?.mode === 'ROUND_TRIP') return 'Round-Trip активний. Вихід при +0.10R або нижче.';
  if (order.position_guard?.mode === 'ADVERSE_CHOCH') return 'Вихід після зламу M15 або просадки від −0.35R при зустрічній структурі.';
  if (order.position_guard?.mode === 'STALE_TIMEOUT') return 'Тайм-стоп вичерпано. Чекаємо підтвердження дії за журналом.';
  if (order.position_guard?.mode === 'STALE_TIGHTENED') return 'Тайм-стоп застосовано. Чинний SL ' + format(order.stop) + '.';
  return 'Зберігаємо поточний стоп до TP1 ' + format(order.tp1) + '.';
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
    (event.kind === 'CLOSED' && number(event.price) ? ' @ ' + format(event.price) :
      number(event.entry) ? ' ' + format(event.entry) : '') + (number(event.net_r) ? ' · ' + signed(event.net_r) + 'R' : '') +
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
function setBannerMode(mode) {
  const banner = $('state-banner');
  banner.dataset.mode = mode;
  for (const name of ['waiting','entered','managing','closed']) {
    banner.classList.toggle('banner-' + name, mode === name.toUpperCase());
  }
  return banner;
}
function renderBanner(price, usable, live) {
  const active = state.orders.find(order => order.status === 'OPEN');
  const pending = state.orders.find(order => order.status === 'PENDING');
  const candidate = chartPlan();
  const closed = state.last_closed;
  let mode = 'WAITING';
  let pill = state.primary_bias ? 'ОЧІКУВАННЯ • ПРІОРИТЕТ ' + sideLabel(state.primary_bias) : 'ПОШУК ТОЧКИ ВХОДУ';
  let title = 'ОЧІКУЄМО УГОДУ' +
    (state.primary_bias ? ' • Пошук за трендом (' + sideLabel(state.primary_bias) + ')' : '');
  let detail = 'Чекаємо підтвердженого сетапу.';
  if (candidate) {
    if (state.cockpit_schema === 5) {
      title = (candidate.market_now ? 'СИГНАЛ MARKET ' : 'ГОТУЄМО ') + sideLabel(candidate.side) + ' ВІД ' +
        format(candidate.entry) + ' • ' + candidate.timeframe + ' ' + zoneName(candidate.kind);
      detail = 'Зона ' + format(candidate.low) + '–' + format(candidate.high) + ' · Δ ' +
        signed(candidate.entry - price, ' $') + ' (' + signed((candidate.entry-price)*100, '¢') + ') · ' +
        planText(candidate) + ' · ' + scoreText(candidate) + '. ' + conditionText(candidate, price);
    } else {
      detail = 'Чекаємо підходу до ' + format(candidate.entry) + '.';
    }
  }
  if (active) {
    const tracker = orderTracker(active);
    const managing = active.tp1_partial === true;
    mode = managing ? 'MANAGING' : 'ENTERED';
    pill = managing ? 'В СУПРОВОДІ • КОНТРОЛЬ ПОЗИЦІЇ' : 'В УГОДІ • ПОЗИЦІЮ ВІДКРИТО';
    const entryType = active.order_type ?? active.type;
    title = (managing ? 'В СУПРОВОДІ' : 'В УГОДІ') + ' • ' +
      (entryType ? entryType + ' ' : '') + sideLabel(active.side) + ' @ ' + format(active.entry);
    detail = management(active, tracker, price, usable);
  } else if (pending) {
    const tracker = orderTracker(pending);
    pill = 'ЛІМІТКА ВИСТАВЛЕНА';
    title = 'ОЧІКУЄМО ВХІД • LIMIT ' + sideLabel(pending.side) + ' @ ' + format(pending.entry);
    detail = 'Очікуємо виконання лімітки.';
    if (tracker.touch) detail = 'Ціна торкнулася входу, чекаємо підтвердження виконання.';
    if (tracker.end) detail = management(pending, tracker, price, usable);
    if (!pendingIsCurrent(pending, price)) {
      title = 'ЧЕКАЄМО ЗВІРКИ ЛІМІТКИ';
      detail = targetExhausted(pending, price) ?
        'Першу ціль уже пройдено. Статус лімітки уточнюється за журналом.' :
        'Рівень лімітки вийшов за робочу відстань. Чекаємо оновлення журналу.';
    }
  } else if (state.orders.length) {
    title = 'СКАСУВАННЯ ЛІМІТКИ';
    detail = 'Чекаємо підтвердження скасування за журналом.';
  } else if (closed?.kind === 'CLOSED' && number(closed.ts) &&
             closed.ts <= state.snapshot_ts && Date.now() - closed.ts < 15 * 60_000) {
    mode = 'CLOSED';
    pill = 'УГОДУ ЗАКРИТО • ФІКСАЦІЯ РЕЗУЛЬТАТУ';
    title = 'УГОДУ ЗАКРИТО • ' + sideLabel(closed.side);
    detail = eventText(closed) + ' о ' + stamp(closed.ts) + '.';
  }
  if (!usable) {
    if (mode === 'WAITING') {
      pill = 'СИГНАЛИ ПРИЗУПИНЕНО';
      title = state.status === 'DATA_HOLD' ? 'ПЕРЕВІРКА РИНКОВИХ ДАНИХ' : 'ЧЕКАЄМО ОНОВЛЕННЯ СТАНУ';
    }
    detail = 'Дані очікують оновлення. Останній зліпок о ' + stamp(state.snapshot_ts) + '.';
  }
  const banner = setBannerMode(mode);
  const isNear=Boolean(usable && live && !active && pending?.status==='PENDING' && number(pending.entry) && pending.entry>0 &&
    pendingIsCurrent(pending, price) && !orderTracker(pending).touch && !orderTracker(pending).end && Math.abs(price-pending.entry)<=.08000001);
  banner.classList.toggle('near',isNear);
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
  radarRows(price).forEach((approved,index) => {
    const side=index===0 ? 1 : -1;
    const row = approved;
    const reserve = reserveLevel(side, price, row);
    const card=element('article',null,'candidate'); card.dataset.side=side;
    card.append(element('h3',sideLabel(side),side===1?'long':'short'));
    card.dataset.priority=state.primary_bias===side?'PRIMARY':state.primary_bias?'COUNTER':'RANGE';
    if (!row) {card.append(element('p',state.primary_bias&&state.primary_bias!==side?
      'Очікуємо підтвердженої зміни тренду.':
      (side === 1 ? 'Сканер шукає підтримку нижче ціни.' : 'Сканер шукає опір вище ціни.'),
      'reason'));
      if (reserve) card.append(element('p', 'Далекий резерв @ ' + format(reserve.entry), 'candidate-reserve'));
      container.append(card);return;}
    card.dataset.reaction=reactions.get(reactionKey(row))?.stage??'WAIT';
    if (state.cockpit_schema === 5) {
      const tag = row.limit_ready ? 'Рівень допущено до лімітки' :
      row.reversal_override ? 'Підтверджений розворот' : 'Допущений план';
      card.append(element('p', tag + ' · ' + row.timeframe + ' ' + zoneName(row.kind) +
        ' · ' + scoreText(row), 'candidate-status'),
        element('p',format(row.entry),'candidate-entry'),
        element('p','Зона ' + format(row.low) + '–' + format(row.high) + ' · Δ ' +
          signed(row.entry-price, ' $') + ' (' + signed((row.entry-price)*100,'¢') + ')','reason'),
        element('p',planText(row),'reason'),
        element('p',conditionText(row,price),'reason'),
        copyButtons(row));
      if (reserve && (!state.primary_bias || state.primary_bias === side)) {
        card.append(element('p','Резерв ' + (side === 1 ? 'нижче' : 'вище') + ' · ' +
          zoneName(reserve.kind) + ' @ ' + format(reserve.entry) + ' · Стоп ' + format(reserve.stop) +
          ' · Тейк 1 ' + format(reserve.tp1) + ' · ' + scoreText(reserve),'candidate-reserve'));
      }
    } else {
      card.append(element('p', format(row.entry), 'candidate-entry'),
        element('p', (row.tactic === 'REACTION_M5' ? 'Чекаємо реакції M5' : 'Чекаємо підходу до входу') +
          ' · клас ' + row.grade, 'reason'),
        element('p', 'SL ' + format(row.stop) + ' · TP1 ' + format(row.tp1) + ' · TP2 ' + format(row.tp2), 'reason'));
    }
    container.append(card);
  });
}
function renderScenarios(usable) {
  const panel = $('scenario-plan');
  if (!panel) return;
  panel.replaceChildren();
  const map = usable ? state.scenario_plan : null;
  for (const [label, letter] of [['LONG','А'],['SHORT','Б']]) {
    const row = map?.[label];
    const card = element('div');
    card.append(element('strong', letter + ' · ' + label));
    const zone = row?.poi;
    card.append(element('p', zone ? 'Ретест ' + format(zone.low) + '–' + format(zone.high) +
      (row.target ? ' → ' + format(row.target.level) : '') : 'Чекаємо підтверджену зону'));
    if (number(row?.breakout_level)) {
      card.append(element('small', 'Альтернатива · пробій ' + format(row.breakout_level) + ' і ретест'));
    }
    panel.append(card);
  }
}

function render() {
  if (!state) return;
  const focusId=document.activeElement?.dataset.focus;
  const now=Date.now(), live=quote && quote.ts<=now+3000 && now-quote.ts<=QUOTE_MAX_AGE;
  const price=displayedPrice(), usable=ready(now);
  renderScenarios(usable);
  const market=usable && live && !document.hidden ? quote : null;
  $('price').textContent=format(price);
  $('quote-status').textContent=live ? 'Live · ' + stamp(quote.ts) : (feedError ? 'OKX недоступний' : 'Котирування застаріло') + ' · зліпок';
  $('stamp').textContent=stamp(state.snapshot_ts); $('instrument').textContent=state.instrument;
  $('regime').textContent=({BULLISH_PULLBACK_OPPORTUNITY:'Висхідний · відкат',
    BEARISH_PULLBACK_OPPORTUNITY:'Низхідний · відкат'})[state.trend_phase] ?? ({BULLISH_ACTIVE:'Висхідний',BEARISH_ACTIVE:'Низхідний',RANGE:'Діапазон',PULLBACK_PHASE:'Відкат'})[state.market_regime] ?? 'Невідомий';
  const structureSync=state.structure_sync;
  $('regime').title=structureSync?.proof === 'BEARISH_BREAKDOWN_MSS'
    ? 'Закритий ведмежий пробій M15'
    : structureSync?.proof === 'DISPLACEMENT_MSS_FVG'
    ? 'Свіп / межа ADR · MSS M15 · FVG'
    : structureSync?.proof === 'M15_BOS_75_ADR'
    ? 'Пробій M15 ' + format(structureSync.level) + ' · хвиля ' + Math.round(structureSync.wave_adr * 100) + '% ADR'
    : '';
  $('state-status').textContent=usable ? 'Журнал актуальний' : state.status==='DATA_HOLD' ? 'Дані на перевірці' : 'Потрібен новий зліпок';
  $('capacity').textContent=(state.occupied_slots ?? state.orders.length) + '/' + (state.max_new_pending ?? 2) + ' місць';
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
    card.append(element('h3',(order.number ? '#'+order.number+' · ' : '') + sideLabel(order.side) + ' · ' + (order.order_type ?? order.type) + ' @ ' + format(order.entry)),
      element('p',order.status==='OPEN' ? 'Позиція за журналом' : order.status==='CANCELLING' ? 'Скасування за журналом' : 'Лімітка за журналом','order-label'));
    if (order.status==='OPEN') card.append(element('p',signed(move,' $/од.') + (order.risk>0 ? ' · '+signed(move/order.risk)+'R' : ''), 'pnl '+(move>=0?'long':'short')),
      element('small',(live ? 'Live' : 'Зліпок') + ' · ціновий рух без комісій; грошовий PnL залежить від обсягу'), lifecycle(order,price));
    else card.append(proximity(order,price));
    card.append(element('p',management(order,tracker,price,usable),'action'));
    const details=element('dl');
    const rr=order.risk>0 && number(order.tp1) ? order.side*(order.tp1-order.entry)/order.risk : null;
    const fields=[['Сетап',order.setup ?? 'Не вказано'],['Бал / клас за журналом',scoreText(order)],['Entry / SL',format(order.entry)+' / '+format(order.stop)],
      ['TP1 / TP2',format(order.tp1)+' / '+format(order.tp2)],['Ризик ціни / R:R TP1',format(order.risk)+' $/од. / '+(number(rr)?rr.toFixed(2):'Не розраховано')],
      ['Захист після TP1', number(order.be_stop) ? format(order.be_stop) : 'Очікує підтвердження M5']];
    if (organicPlan(order)) fields.push(['Розрахунок входу', planText(order)]);
    if (order.cancel_reason === 'LEGACY_MICRO_RISK') fields.push(['Причина скасування', 'Перехід на органічний стоп']);
    if (number(order.pending_stop)) fields.push(['Запланований SL',format(order.pending_stop)+' з '+stamp(order.pending_stop_effective_ts)]);
    for (const [key,value] of fields) details.append(element('dt',key),element('dd',value));
    if (order.status === 'OPEN') positionMetrics(details, order, tracker, now);
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
  renderBanner(price,usable,live); renderRadar(price,live); renderReaction(usable,live); renderZones(price); renderActivity();
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
    upColor: '#4bd2a5', downColor: '#f17785', borderVisible: false, priceLineVisible: false,
    wickUpColor: '#4bd2a5', wickDownColor: '#f17785',
    autoscaleInfoProvider: original => {
      const info=original();
      if (!info || !state) return info;
      const values=chartOrders().flatMap(o=>[o.entry,o.stop,o.tp1,o.tp2]).filter(number);
      const main=chartPlan();
      if (main) values.push(main.entry,main.stop,main.tp1);
      for (const zone of visibleZones()) values.push(zone.low, zone.high);
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
      const focusId = chartPlan()?.id;
      for (const zone of visibleZones()) {
        const y1 = series.priceToCoordinate(zone.high), y2 = series.priceToCoordinate(zone.low);
        const floor = Math.floor((zone.known_ts ?? 0) / (chartFrames[frame] * 1000)) * chartFrames[frame];
        if (floor > (currentBar?.time ?? history.at(-1).time)) continue;
        const x = floor <= history[0].time ? scale.timeToCoordinate(history[0].time) : scale.timeToCoordinate(floor);
        if (y1 === null || y2 === null) continue;
        const left = Math.max(0, x ?? 0);
        if (left >= mediaSize.width-30) continue;
        context.fillStyle = zone.id === focusId ? 'rgba(92,163,255,.18)' :
          zone.side === 1 ? 'rgba(56,195,159,.13)' : 'rgba(255,112,132,.13)';
        const top=Math.min(y1,y2), bottom=Math.max(y1,y2);
        context.fillRect(left, top, Math.max(0, mediaSize.width-left), Math.max(1,bottom-top));
        if (zone.id===selectedZone) {
          context.strokeStyle='#92dff0'; context.lineWidth=2;
          context.strokeRect(left+1,top,Math.max(0,mediaSize.width-left-2),Math.max(1,bottom-top));
        }
        const focus = zone.id === focusId;
        const label = (focus ? (zone.actionable ? 'ЦІЛЬ ' : 'ОРІЄНТИР ') + sideLabel(zone.side) + ' · ' : '') +
          (zone.timeframe ?? '') + ' ' + zoneName(zone.kind) + ' [' + format(zone.low) + '–' + format(zone.high) + ']' +
          (number(zone.score) ? ' · ' + scoreText(zone) : '');
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
  const specs=[];
  const add=(price,title,color,dashed=true) => {
    if (number(price)) specs.push({price,title,color,lineWidth:1,lineStyle:dashed?style.Dashed:style.Solid});
  };
  const price=displayedPrice();
  for (const order of chartOrders()) {
    const prefix=(order.number ? '#'+order.number+' ' : '')+sideLabel(order.side)+' ';
    add(order.entry,prefix+(order.status==='OPEN'?(order.order_type ?? order.type ?? 'В УГОДІ'):order.status==='CANCELLING'?'СКАСУВАННЯ':'LIMIT')+
      ' @ '+format(order.entry)+(order.status==='PENDING'?' · Δ '+signed(order.entry-price,' $'):''),'#55d6ad');
    add(order.stop,prefix+'SL @ '+format(order.stop)+' ('+signed(order.side*(order.stop-order.entry)/order.risk)+'R)','#ff8994',false);
    add(order.tp1,prefix+'TP1 · '+Math.round(order.tp1_fraction*100)+'% ('+signed(order.side*(order.tp1-order.entry)/order.risk)+'R)','#7db7ff');
    if (order.tp2!==order.tp1) add(order.tp2,prefix+'TP2 · залишок ('+signed(order.side*(order.tp2-order.entry)/order.risk)+'R)','#7db7ff');
  }
  const candidate = chartPlan();
  if (candidate) {
    add(candidate.entry, (candidate.market_now ? 'СИГНАЛ MARKET ' : 'ОЧІКУЄМО ВХІД ') + sideLabel(candidate.side) + ' @ ' + format(candidate.entry) +
      ' (Δ ' + signed(candidate.entry - price,' $') + ')', '#589aff');
    add(candidate.stop, 'СТОП-ЛОС @ ' + format(candidate.stop) + ' (' + signed(-candidate.side * (candidate.entry - candidate.stop),' $') + ')', '#ff8994', false);
    add(candidate.tp1, 'ТЕЙК-ПРОФІТ 1 @ ' + format(candidate.tp1) + ' (' + signed((candidate.tp1 - candidate.entry) / (candidate.entry - candidate.stop),'R') + ')', '#7db7ff');
  }
  series.applyOptions({priceFormat:{type:'price',precision:Math.max(2,(String(state.tick).split('.')[1] ?? '').length),minMove:state.tick || .01}});
  series.applyOptions({lastValueVisible:!candidate});
  const labels=candidate?[]:[price,(currentBar??history.at(-1))?.close].filter(number).map(p=>series.priceToCoordinate(p)).filter(y=>y!==null);
  const legend=[];
  for (const spec of specs) {
    const y=series.priceToCoordinate(spec.price);
    const visible=y!==null&&!labels.some(old=>Math.abs(y-old)<20);
    if (visible) labels.push(y);
    priceLines.push(series.createPriceLine({...spec,axisLabelVisible:visible}));
    const item=element('span',spec.title.includes(format(spec.price))?spec.title:spec.title+' @ '+format(spec.price)); item.style.color=spec.color;legend.push(item);
  }
  $('chart-levels').replaceChildren(...legend);
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
  const price = positiveNumber(row?.last), ts = positiveNumber(row?.ts);
  if (row?.instId !== state?.instrument || !number(price) || price <= 0 ||
      !number(ts) || ts <= 0 || ts > Date.now() + 3000 || (quote && ts < quote.ts)) return false;
  if (quote&&ts===quote.ts) return price===quote.price;
  quote = { price, ts };
  observeLive(quote);
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
      position.clear(); selectedZone=null; reactions.clear(); m5Proofs.clear(); invalidZones.clear(); exhaustedPlans.clear(); selectedPlanIds.clear(); m5Market=null; previousLiveSample=null;
      disconnectSocket();
    }
    const previousSnapshot = state?.snapshot_ts;
    state = next;
    const knownZones = new Set([...poolRows().map(zoneFingerprint), ...next.orders.map(exhaustionKey)]);
    for (const cache of [invalidZones, exhaustedPlans]) {
      for (const fingerprint of cache) if (!knownZones.has(fingerprint)) cache.delete(fingerprint);
    }
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
    const keep=new Set(reactionRows().map(reactionKey));
    for (const [key,t] of reactions) if (!keep.has(key)&&!['INVALID','MISSED'].includes(t.stage)) reactions.delete(key);
    render();
    void pollTicker(); void pollM5();
    await loadCandles(first);
  } catch {
    stateError = true;
    $('state-status').textContent = 'Зліпок бота недоступний';
    if (!state) {
      setBannerMode('WAITING'); $('state-title').textContent='СТАН БОТА НЕДОСТУПНИЙ';
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

$('zones').addEventListener('click', event => {
  const button=event.target.closest('button[data-zone]');
  if (!button || chartPlan()?.id !== button.dataset.zone) return;
  selectedZone=selectedZone===button.dataset.zone ? null : button.dataset.zone;
  chart?.priceScale('right').applyOptions({autoScale:true});
  render();
  if (window.innerWidth<=760) $('chart-title').scrollIntoView({block:'center',behavior:'smooth'});
});
async function copyLevel(event) {
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
}
$('orders').addEventListener('click', copyLevel);
$('radar').addEventListener('click', copyLevel);
initChart();
void pollState();
setInterval(() => {
  if (!state || document.hidden) return;
  for (const node of document.querySelectorAll('[data-expiry]')) node.textContent=countdown(Number(node.dataset.expiry));
  const price=displayedPrice(), now=Date.now();
  for (const node of document.querySelectorAll('[data-trade-entered]')) tradeDeadline(node, now);
  const live=quote && now-quote.ts<=QUOTE_MAX_AGE && quote.ts<=now+3000;
  renderBanner(price,ready(now),live); renderReaction(ready(now),live);
},1000);
setInterval(pollTicker, 3000);
setInterval(pollState, 30_000);
setInterval(pollM5,3000);
document.addEventListener('visibilitychange', () => {
  pauseReactions();
  if (!document.hidden) { void pollState(); void pollTicker(); void pollM5(); }
});
