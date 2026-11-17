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
let chartPosition, chartLevels = [], priceFlash;
let history = [], currentBar, lastSampleTime = 0, audio, sound = false, feedError = false;
let socket, socketAttempt = 0, socketPing;
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
function scoreText(row) {
  return number(row.score) ? formatScore(row.score) + '/100' + (row.grade ? ' ' + row.grade : '') : '—';
}
const formatScore = n => Number(n.toFixed(1)).toString();
function organicPlan(row) {
  return row.organic_risk?.model === 'ORGANIC_OIL_V32';
}
function targetRatiosValid(row, rr1, rr2) {
  return organicPlan(row) ?
    rr1 >= (state.candidate_rules?.min_rr ?? 1.40) - 1e-8 &&
    rr1 <= (state.candidate_rules?.max_rr1 ?? 1.45) + 1e-8 && rr2 >= 1.8 - 1e-8 && rr2 <= 2.25 + 1e-8 :
    rr1 >= 1.4 - 1e-8 && rr2 >= 2.2 - 1e-8;
}
function marketCandidate(row) {
  return row.order_type === 'MARKET' &&
    (['M15_LEVEL_REJECTION','M15_BREAKDOWN_MOMENTUM','M15_TREND_ZONE','M15_STRUCTURAL_IMPULSE','M5_ZONE_ENGULFING','M5_SNIPER_REVERSAL','TREND_CONTINUATION_MARKET'].includes(row.market_trigger?.pattern) || (row.trend_rel === 'WITH_TREND' && ['IN_ZONE_TOUCH','POST_TOUCH_REJECTION','BREAKOUT_CONTINUATION'].includes(row.market_trigger?.pattern)) ||
     (row.trend_rel === 'CONFIRMED_REVERSAL' && row.market_trigger?.pattern === 'CONFIRMED_REVERSAL'));
}
function validCandidate(row) {
  if (!row || ![1,-1].includes(row.side) || !['15m','1H','4H'].includes(row.timeframe)) return false;
  const locked={DEMAND_OB:1,FVG_BULL:1,BULLISH_BREAKER:1,BULL_BREAKER:1,BREAKER_BULL:1,SWEEP_LOW:1,IFVG_BULL:1,
    SUPPLY_OB:-1,FVG_BEAR:-1,BEARISH_BREAKER:-1,BEAR_BREAKER:-1,BREAKER_BEAR:-1,SWEEP_HIGH:-1,IFVG_BEAR:-1};
  const neutral=['CONFIRMED_FVG','BREAKER_FLIP_15M','MOMENTUM_STAIRCASE_15M','DISPLACEMENT_CATCHER_15M',
    'LIQUIDITY_RAID_ENGULFING_15M','STRUCTURAL_LEVEL','SESSION_SWEEP_ENGULFING',
    'SESSION_SWEEP_ENGULFING_15M','SWEEP_RECLAIM_ZONE','SESSION_OB','IMPULSE_OTE_15M',
    'FAILED_BREAKOUT_TRAP','V_REVERSAL_CHOCH','EMA9_MOMENTUM_SURF','SWEEP_REVERSAL','M15_CHOCH','BREAKER_CHOCH','MULTIBAR_SWEEP','RECLAIM_BREAKER','SR_FLIP_RECLAIM','BREAKDOWN_RECLAIM','CONTINUATION_PULLBACK','M5_IMPULSE_OB','FLAG_RETEST',
    'BREAKER_BLOCK','FVG_PULLBACK','FIB_OTE_ZONE','SWEEP_CONTINUATION','BOS_CONTINUATION',
    'CONSOLIDATION_BREAKOUT_BASE','VWAP_TREND_CONTINUATION','M5_SNIPER_REVERSAL','BOS_IMPULSE_BREAKOUT'];
  if (locked[row.kind] ? locked[row.kind]!==row.side : !neutral.includes(row.kind)) return false;
  if (!['low','high','entry','stop','tp1','tp2'].every(k=>number(row[k])&&row[k]>0)||!number(row.score)||row.score<0) return false;
  const risk=row.side*(row.entry-row.stop);
  const proximal=row.side===1?row.high:row.low;
  const outside=row.entry<row.low || row.entry>row.high;
  const withinZone = marketCandidate(row) ?
    (row.market_trigger.sniper_pullback ? pullbackQuoteAllowed(row, row.entry) :
    ['M15_LEVEL_REJECTION','M15_BREAKDOWN_MOMENTUM','M15_TREND_ZONE','M15_STRUCTURAL_IMPULSE','M5_ZONE_ENGULFING','M5_SNIPER_REVERSAL','TREND_CONTINUATION_MARKET'].includes(row.market_trigger.pattern) ?
      Math.abs(row.entry - row.market_trigger.limit_entry) <= (row.market_trigger.max_distance ?? .20*row.market_trigger.initial_risk) + 1e-8 :
      row.market_trigger.pattern === 'CONFIRMED_REVERSAL' ? !outside :
      row.market_trigger.pattern === 'IN_ZONE_TOUCH' ?
      row.entry >= row.low - .08000001 && row.entry <= row.high + .08000001 :
      row.side * (row.entry - proximal) >= 0 && row.side * (row.entry - proximal) <= .45 * risk + 1e-8) :
    (!outside || (row.entry_basis === 'SHALLOW_M15_RETEST' && number(row.execution_low) &&
      number(row.execution_high) && row.entry >= row.execution_low && row.entry <= row.execution_high) ||
      (row.side * (row.entry - proximal) >= 0 &&
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
function pullbackQuoteAllowed(row, price) {
  const level = row.market_trigger?.limit_entry ?? row.entry;
  const tolerance = row.market_trigger?.max_distance ?? row.market_pullback_max;
  const distal = row.side === 1 ? row.low : row.high;
  return number(price) && number(tolerance) && row.side * (price-distal) >= -1e-8 &&
    Math.abs(price-level) <= tolerance + 1e-8;
}
function actionablePlan(row, price, tracking = false) {
  if (row.actionable !== true || row.execution_admitted !== true || !['A','B+'].includes(row.grade) ||
      row.status !== 'CANDIDATE' || row.gate_reason || !['LIMIT','REACTION_M5','MARKET'].includes(row.tactic) ||
      !followsBias(row) || !validCandidate(row) ||
      (state.organic_risk_profile?.model === 'ORGANIC_OIL_V32' && !organicPlan(row))) return false;
  const stage = livePlanState(row, price);
  if (marketCandidate(row)) {
    if (row.market_trigger.sniper_pullback)
      return !['INVALID','EXPIRED','MISSED','EXHAUSTED'].includes(stage) && pullbackQuoteAllowed(row, price);
    if (['M15_TREND_ZONE','M15_STRUCTURAL_IMPULSE','M5_ZONE_ENGULFING','M5_SNIPER_REVERSAL','TREND_CONTINUATION_MARKET'].includes(row.market_trigger.pattern))
      return !['INVALID','EXPIRED','MISSED','EXHAUSTED'].includes(stage) &&
        number(price) && Math.abs(price-row.market_trigger.limit_entry) <= row.market_trigger.max_distance + 1e-8;
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
    const spent = row.status === 'PENDING' ? pendingTargetReached(row, market.price) :
      number(row.tp1) && row.side * (market.price - row.tp1) >= -1e-8;
    if (spent) {
      exhaustedPlans.add(exhaustionKey(row));
    }
  }
  const keep=new Set();
  for (const row of reactionRows()) {keep.add(reactionKey(row));updateReaction(row,market);}
  for (const [key,t] of reactions) if (!keep.has(key)&&!['INVALID','MISSED'].includes(t.stage)) reactions.delete(key);
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
  const trendMarket = row.market_trigger?.pattern === 'TREND_CONTINUATION_MARKET';
  const strictPullback = number(row.market_pullback_max) && !trendMarket;
  const projected = !strictPullback && !trendMarket && row.institutional_family && row.target_projection;
  const tp1 = projected?.tp1 ? price + row.side*1.45*risk : row.tp1;
  const tp2 = projected?.tp2 ? price + row.side*1.90*risk : row.tp2;
  const rr = risk > 0 ? row.side * (tp1 - price) / risk : 0;
  const rr2 = risk > 0 ? row.side * (tp2 - price) / risk : 0;
  const costs = (price + tp2) * (settings.taker_fee ?? 0);
  const plannedRisk = row.side * (row.entry - row.stop);
  const move = row.side * (price - row.entry);
  const maxChase = (settings.max_chase_r ?? .25) * plannedRisk;
  const floor = organicPlan(row) ? row.organic_risk.floor ?? Math.max(1.15, Math.round(price*1.25)/100) : row.risk_floor ?? .22;
  const cap = organicPlan(row) ? row.organic_risk.cap ?? Math.max(2.10, Math.round(price*2.2)/100) : row.risk_cap ?? .65;
  const near = trendMarket ? Math.abs(price-row.market_trigger.limit_entry) <= row.market_trigger.max_distance+1e-8 :
    strictPullback ? pullbackQuoteAllowed(row, price) : move >= 0 && move <= maxChase;
  const minTrendR = trendMarket ? 1.40 : 0;
  if (rr < minTrendR - 1e-8 || !near || risk < floor - 1e-8 || risk > cap + 1e-8 ||
      !targetRatiosValid(row, rr, rr2) ||
      (organicPlan(row) ? rr <= (price + tp1) * (settings.taker_fee ?? 0) / risk :
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

const brainMinute = 60_000;
const brainQuarter = 15 * brainMinute;
const brain = {series: {}, rows: [], orders: [], used: new Set(), sourceTs: 0, lastTs: 0, computed: 0};
const liveBrainEnabled = () => state?.brain_config?.schema === 1;
const liveBrainFresh = (now = Date.now()) => liveBrainEnabled() && now - brain.computed < QUOTE_MAX_AGE;
const displayOrders = () => liveBrainEnabled() && brain.sourceTs === state.snapshot_ts ? brain.orders : state.orders;
const newYorkClock = new Intl.DateTimeFormat('en-US', {
  timeZone:'America/New_York', weekday:'short', hour:'2-digit', minute:'2-digit', hourCycle:'h23'
});

function brainSessionAllowed(ts) {
  const utc = new Date(ts).getUTCHours();
  const ny = Object.fromEntries(newYorkClock.formatToParts(ts).map(p => [p.type,p.value]));
  const minute = Number(ny.hour)*60 + Number(ny.minute);
  return utc >= 1 && utc < 22 &&
    !(!['Sat','Sun'].includes(ny.weekday) && minute >= 570 && minute < 585) &&
    !(ny.weekday === 'Wed' && minute >= 615 && minute < 645);
}

function brainCandles(raw, step, now) {
  const rows = new Map();
  for (const item of raw) {
    const [ts,open,high,low,close,volume] = item.slice(0,6).map(Number);
    if (String(item[8]) === '1' && [ts,open,high,low,close,volume].every(number) &&
        ts > 0 && ts % step === 0 && ts+step <= now && low > 0 && volume >= 0 &&
        low <= Math.min(open,close) && high >= Math.max(open,close))
      rows.set(ts,{ts,open,high,low,close,volume});
  }
  return [...rows.values()].sort((a,b) => a.ts-b.ts);
}

function brainAtr(rows) {
  const start=Math.max(1,rows.length-14);
  const ranges=rows.slice(start).map((b,i)=>{
    const prev=rows[start+i-1].close;
    return Math.max(b.high-b.low,Math.abs(b.high-prev),Math.abs(b.low-prev));
  });
  return ranges.length ? ranges.reduce((sum,tr)=>sum+tr,0)/ranges.length : 0;
}

function brainPivots(rows, strength, step) {
  const points=[];
  for (let i=strength;i<rows.length-strength;i++) {
    const bar=rows[i], others=[...rows.slice(i-strength,i),...rows.slice(i+1,i+strength+1)];
    if (others.every(b=>bar.high>b.high)) points.push({side:-1,price:bar.high,known_ts:rows[i+strength].ts+step});
    if (others.every(b=>bar.low<b.low)) points.push({side:1,price:bar.low,known_ts:rows[i+strength].ts+step});
  }
  return points;
}

function brainLiquidity(points, entry) {
  const levels={};
  for (const [label,side] of [['bsl',-1],['ssl',1]]) {
    const closest=points.filter(p=>p.side===side && side*(entry-p.price)>0)
      .sort((a,b)=>Math.abs(a.price-entry)-Math.abs(b.price-entry))[0];
    if (closest) levels[label]={price:closest.price,known_ts:closest.known_ts};
  }
  return levels;
}

function brainIndicators(rows, minutes, market) {
  if (rows.length < 50 || rows.at(-1).ts + brainQuarter > market.ts ||
      market.ts - rows.at(-1).ts - brainQuarter >= brainQuarter ||
      rows.some((bar,i) => i && bar.ts - rows[i-1].ts !== brainQuarter)) return null;
  const ema=(period,bars)=>{
    const seed=bars.slice(0,period).reduce((total,bar)=>total+bar.close,0)/period;
    return bars.slice(period).reduce((value,bar)=>value+(bar.close-value)*2/(period+1),seed);
  };
  const end = rows.at(-1).ts+brainQuarter, midnight = Math.floor(end/86_400_000)*86_400_000;
  const quarters = rows.filter(b => b.ts >= midnight);
  // Use a complete daily prefix. Prefer minute data where the full quarter exists.
  const fine = new Map(minutes.map(b => [b.ts,b]));
  const day = quarters.flatMap(b => {
    const chunk = Array.from({length:15},(_,i)=>fine.get(b.ts+i*brainMinute));
    return chunk.every(Boolean) ? chunk : [b];
  });
  const vwapOf=bars=>{
    const volume=bars.reduce((total,b)=>total+b.volume,0);
    return volume>0 ? bars.reduce((total,b)=>total+(b.high+b.low+b.close)/3*b.volume,0)/volume : null;
  };
  const covered=quarters[0]?.ts===midnight || end===midnight;
  const referenceVwap=covered ? vwapOf(day) : null;
  // The directional matrix uses the last closed M15 boundary, as in Python.
  const minuteEnd=Math.floor(market.ts/brainMinute)*brainMinute;
  let cursor=end;
  for (;cursor<minuteEnd && fine.has(cursor);cursor+=brainMinute) day.push(fine.get(cursor));
  const vwap=covered && cursor===minuteEnd ? vwapOf(day) : referenceVwap;
  const ema9=ema(9,rows.slice(-120)), ema20=ema(20,rows.slice(-60)), ema50=ema(50,rows.slice(-120));
  const close=rows.at(-1).close;
  const side = [1,-1].find(s => number(referenceVwap) && s*(close-ema20)>0 &&
    s*(close-referenceVwap)>0 && s*(ema9-ema20)>0) ?? 0;
  const liveEma = (value,period) => value+(market.price-value)*2/(period+1);
  const rangeBars=rows.slice(-192,-1);
  const range={low:Math.min(...rangeBars.map(b=>b.low)),high:Math.max(...rangeBars.map(b=>b.high))};
  return {ema9,ema20,ema50,vwap,referenceVwap,atr:brainAtr(rows),side,range,close,
    liveEma9:liveEma(ema9,9),liveEma20:liveEma(ema20,20),liveEma50:liveEma(ema50,50)};
}

function scanLiveCandlesInBrowser(rows, m5, market, matrix, config, tick, minutes=[]) {
  if (matrix.atr <= 0) return [];
  const {atr,side:trend} = matrix;
  const result=new Map(), last=rows.at(-1), prior=rows.slice(-13,-1);
  const {low:rangeLow,high:rangeHigh}=matrix.range??{};
  const wick=(row,side)=>side===1?row.low:row.high;
  const far=(row,side)=>side===1?row.high:row.low;
  const roundOut=(price,side)=>Math.round((side===1?
    Math.ceil(price/tick-1e-8):Math.floor(price/tick+1e-8))*tick*1e8)/1e8;
  const m5step=5*brainMinute;
  const closures=brainPivots(rows.slice(-96),2,brainQuarter);
  const unbroken=closures.filter(point=>!rows.some(b=>b.ts>=point.known_ts &&
    point.side*(b.close-point.price)<0));
  const common={marketRadius:Math.min(.18,config.market_touch_atr??.18)*atr,
    limitRadius:(config.limit_approach_atr??.85)*atr};
  const add=(kind,side,ref,anchor,bar,step,{breakout=false,formation=bar.ts,
      confirmLevel=ref,zoneLow=Math.min(ref,anchor),zoneHigh=Math.max(ref,anchor)}={})=>{
    const known=bar.ts+step;
    if (known>market.ts || market.ts-known>=3*brainQuarter || zoneHigh-zoneLow<tick-1e-8 ||
        (trend && trend!==side)) return;
    if (rows.some(b=>b.ts>=known && side*(wick(b,side)-anchor)<-tick)) return;
    const structuralEntry=roundOut(ref,side);
    const quote=side===1?market.ask:market.bid;
    const quoted=number(market.bid)&&number(market.ask)&&market.bid>0&&market.ask>=market.bid&&
      market.ask-market.bid<=Math.max(2*tick,.15*atr)+1e-8;
    const rangePos=number(rangeLow)&&rangeHigh>rangeLow ? (quote-rangeLow)/(rangeHigh-rangeLow) : null;
    const rangeMiddle=!trend && (rangePos===null || rangePos>=.30 && rangePos<=.70);
    const edge=side===1?rangeHigh:rangeLow;
    const edgeBroken=breakout && side*(bar.close-edge)>tick && side*(quote-edge)>tick;
    const touch=quoted && Math.abs(quote-structuralEntry)<=common.marketRadius+1e-8;
    const impulse=quoted && breakout && market.ts-known<step &&
      side*(quote-confirmLevel)>=tick/2-1e-8 &&
      Math.abs(quote-structuralEntry)<=(config.market_band_atr??.65)*atr+1e-8;
    const wantsMarket=(!rangeMiddle||edgeBroken) && (touch||impulse);
    const stop=roundOut(structuralEntry-side*Math.max(config.risk_floor,
      side*(structuralEntry-anchor)+Math.max(2*tick,.12*atr)),-side);
    const prices=unbroken.filter(p=>p.side===-side && p.known_ts<=known)
      .map(p=>p.price).sort((a,b)=>side*(a-b));
    const tp1=prices.find(level=>side*(level-structuralEntry)>0);
    const chooseEntry=entry=>{
      const risk=side*(entry-stop), rr1=side*(tp1-entry)/risk;
      if (risk<config.risk_floor-1e-8 || risk>config.risk_cap+1e-8 || !number(rr1) ||
          rr1<(config.market_min_rr??1.35)-1e-8) return null;
      const tp2=prices.find(p=>side*(p-entry)/risk>=Math.max(1.85,rr1+tick/risk)) ??
        roundOut(entry+side*Math.max(1.90,rr1+tick/risk)*risk,side);
      return {entry,stop,risk,rr1,tp1,tp2};
    };
    const marketPlan=wantsMarket ? chooseEntry(quote) : null;
    const plan=marketPlan??chooseEntry(structuralEntry);
    if (!plan || side*(market.price-plan.stop)<=0 || side*(market.price-plan.tp1)>=-1e-8) return;
    const gap=side*(quote-structuralEntry);
    const limitReady=quoted && !wantsMarket && !breakout && !touch && gap>=tick-1e-8 &&
      gap<=common.limitRadius+1e-8 && gap<config.missed_target_r*plan.risk &&
      plan.rr1>=(config.limit_min_rr??1.35)-1e-8;
    const normalVolume=prior.reduce((sum,b)=>sum+b.volume,0)/prior.length;
    const score=25+20+Math.min(25,Math.floor(12*plan.rr1))+(trend===side?15:0)+
      (normalVolume>0&&bar.volume>=1.10*normalVolume?8:0);
    if (score<60) return;
    const id=['paper',kind,side,known,ref.toFixed(4)].join(':');
    result.set(id,{...plan,id,kind,setup:kind,side,low:zoneLow,high:zoneHigh,reference:structuralEntry,score,
      known_ts:known,origin_ts:formation,timeframe:step===brainQuarter?'15m':'5m',
      liquidity:brainLiquidity(unbroken,plan.entry),order_type:marketPlan?'MARKET':'LIMIT',
      market_now:!!marketPlan,actionable:true,activation_ready:!!marketPlan||limitReady,
      activation_kind:breakout?'BREAKOUT':'RETEST',activation_level:breakout?confirmLevel:structuralEntry,
      market_radius:common.marketRadius,limit_radius:common.limitRadius,
      range_blocked:rangeMiddle&&!edgeBroken, market_waiting_rr:wantsMarket&&!marketPlan,local:true});
  };
  // Volume-backed BOS on a closed M15 or two contiguous M5 candles.
  for (const [bars,step,count] of [[rows,brainQuarter,1],[m5,m5step,2]]) {
    for (let end=bars.length-1;end>=Math.max(22+count-1,bars.length-3);end--) {
      const leg=bars.slice(end+1-count,end+1), first=leg[0], close=leg.at(-1);
      const before=bars.slice(Math.max(0,end+1-count-40),end+1-count);
      const known=close.ts+step, oldAtr=brainAtr(before), spine=[...before,...leg];
      if (!oldAtr || market.ts-known>=2*brainQuarter ||
          spine.some((b,i)=>i&&b.ts-spine[i-1].ts!==step)) continue;
      const side=close.close>first.open?1:-1, body=side*(close.close-first.open);
      const high=Math.max(...leg.map(b=>b.high)),low=Math.min(...leg.map(b=>b.low));
      const baseline=before.slice(-20).reduce((sum,b)=>sum+b.volume,0)/20;
      const relative=leg.reduce((sum,b)=>sum+b.volume,0)/(count*baseline);
      const asOfRows=rows.filter(b=>b.ts+brainQuarter<=known);
      const asOf=brainIndicators(asOfRows,minutes,{ts:known,price:close.close});
      if (!baseline || relative<1.10-1e-8 || body<Math.max(tick,.65*oldAtr) ||
          leg.some(b=>side*(b.close-b.open)<=0) || !asOf || !number(asOf.referenceVwap) ||
          side*(close.close-asOf.ema20)<=0 || side*(close.close-asOf.referenceVwap)<=0 ||
          high<=low || (side===1?(close.close-low)/(high-low)<.65:(close.close-low)/(high-low)>.35)) continue;
      const pivot=brainPivots(before,1,step).reverse().find(p=>p.side===-side && p.known_ts<=first.ts &&
        side*(before.at(-1).close-p.price)<=tick/2 && side*(first.open-p.price)<=tick/2 &&
        side*(close.close-p.price)>=tick-1e-8);
      if (!pivot || bars.slice(end+1).some(b=>side*(b.close-pivot.price)<-tick/2)) continue;
      const ref=roundOut(close.close-side*Math.min(.25*body,side*(close.close-pivot.price))+tick/2,-1);
      const edge=ref-side*Math.max(tick,Math.min(.10,.10*oldAtr));
      add('BOS_IMPULSE_BREAKOUT',side,ref,side===1?low:high,close,step,
        {breakout:true,confirmLevel:pivot.price,formation:first.ts,zoneLow:Math.min(ref,edge),zoneHigh:Math.max(ref,edge)});
    }
  }
  for (const side of [1,-1]) {
    const extreme=far(last,side), oldEdge=side*Math.max(...prior.map(b=>side*far(b,side)));
    const sweep=-side*Math.max(...prior.map(b=>-side*wick(b,side))), body=side*(last.close-last.open);
    const base=prior.slice(-5);
    const span=Math.max(...base.map(b=>b.high))-Math.min(...base.map(b=>b.low));
    if (span<=1.30*atr && side*(last.close-oldEdge)>tick && body>=.65*atr &&
        side*(extreme-last.close)<=.35*(last.high-last.low))
      add('CONSOLIDATION_BREAKOUT_BASE',side,oldEdge,wick(last,side),last,brainQuarter,
        {breakout:true,formation:base[0].ts});
    const wickEdge=side===1?Math.min(last.open,last.close):Math.max(last.open,last.close);
    const raid=side*(sweep-wick(last,side));
    const wickRatio=side*(wickEdge-wick(last,side))/Math.max(tick,last.high-last.low);
    if (raid>=tick-1e-8 && raid<=Math.max(.20,.8*atr) && side*(wickEdge-sweep)>=-tick/2 &&
        side*(last.close-sweep)>=tick-1e-8 && wickRatio>=.45-1e-8)
      add('TURTLE_SOUP',side,sweep,wick(last,side),last,brainQuarter);
    const [left,neck,right,confirm]=rows.slice(-4);
    if (side*(wick(right,side)-wick(left,side))>=tick &&
        side*(far(neck,side)-wick(right,side))>=.45*atr &&
        side*(right.close-far(neck,side))<=0 && side*(confirm.close-far(neck,side))>tick &&
        side*(confirm.close-confirm.open)>=.30*atr)
      add('V_REVERSAL_CHOCH',side,far(neck,side),wick(right,side),confirm,brainQuarter,{formation:right.ts});
    for (let i=rows.length-3;i<rows.length;i++) {
      const bar=rows[i], mid=rows[i-1], old=rows[i-2];
      if (side*(wick(bar,side)-far(old,side))>.25*atr &&
          !rows.slice(i+1).some(b=>side*(wick(b,side)-far(old,side))<=0))
        add(side===1?'FVG_BULL':'FVG_BEAR',side,wick(bar,side),far(old,side),bar,brainQuarter,{formation:mid.ts});
      if (side*(bar.close-bar.open)>=1.10*atr && side*(mid.close-mid.open)<0)
        add(side===1?'DEMAND_OB':'SUPPLY_OB',side,far(mid,side),wick(mid,side),bar,brainQuarter,{formation:mid.ts});
    }
    const fresh=m5.at(-1), previous=m5.at(-2), impulse=m5.at(-3);
    if (side!==trend || !fresh || previous?.ts!==fresh.ts-m5step || impulse?.ts!==previous.ts-m5step ||
        side*(fresh.close-fresh.open)<tick-1e-8) continue;
    const atOpen=brainIndicators(rows.filter(b=>b.ts+brainQuarter<=fresh.ts),minutes,{ts:fresh.ts,price:fresh.open});
    const atr5=brainAtr(m5.slice(0,-1));
    if (atOpen?.side===side && market.ts-fresh.ts-m5step<m5step &&
        side*(atOpen.ema20-atOpen.ema50)>0 && side*(atOpen.close-atOpen.ema9)>0 &&
        side*(fresh.close-atOpen.ema9)>0 && Math.abs(wick(fresh,side)-atOpen.ema9)<=.35*atr) {
      for (const count of [3,2,1]) {
        const pause=m5.slice(-count-1,-1), start=m5.at(-count-2);
        if (!start || [start,...pause,fresh].some((b,i,chain)=>i&&b.ts-chain[i-1].ts!==m5step) ||
            side*(start.close-start.open)<.50*atr5 || pause.some(b=>
              side*(b.close-atOpen.ema9)<-.35*atr || Math.abs(b.close-b.open)>.80*atr5)) continue;
        const anchor=side*Math.min(...[...pause,fresh].map(b=>side*wick(b,side)));
        const edge=atOpen.ema9-side*Math.max(tick,.10*atr);
        add('EMA9_MOMENTUM_SURF',side,atOpen.ema9,anchor,fresh,m5step,
          {formation:start.ts,zoneLow:Math.min(edge,atOpen.ema9),zoneHigh:Math.max(edge,atOpen.ema9)});
        break;
      }
    }
    const shelf=m5.slice(-9,-3);
    if (shelf.length>=5 && shelf.every((b,i)=>!i||b.ts-shelf[i-1].ts===m5step)) {
      const level=side*Math.max(...shelf.map(b=>side*far(b,side)));
      if (side*(impulse.close-level)>tick && side*(previous.close-level)>=0 &&
          Math.abs(wick(fresh,side)-level)<=.38*atr && side*(fresh.close-level)>tick)
        add(side*(impulse.open-level)>=0?'RECLAIM_BREAKER':'SR_FLIP_RECLAIM',side,
            level,wick(fresh,side),fresh,m5step,{formation:impulse.ts});
    }
  }
  return [...result.values()].sort((a,b)=>Math.abs(a.entry-market.price)-Math.abs(b.entry-market.price)||b.score-a.score);
}

function brainStorageKey(instrument) {
  return 'bzu_live_trade_state:'+ (globalThis.location?.pathname ?? '/') + ':' + instrument;
}

function loadBrain(instrument) {
  try {
    const key=brainStorageKey(instrument), legacy=key.replace('bzu_live_trade_state:','bzu-paper-v14.2:');
    const saved=JSON.parse(globalThis.localStorage?.getItem(key) ??
      globalThis.localStorage?.getItem(legacy) ?? 'null');
    if (!saved || saved.instrument!==instrument || !Array.isArray(saved.orders) ||
        !Array.isArray(saved.canonicalIds) || !Array.isArray(saved.used) ||
        (saved.event ? typeof saved.event.orderId!=='string' || !number(saved.event.ts) ||
          saved.event.ts>Date.now()+5000 || !number(saved.event.candleTs) ||
          saved.event.candleTs!==Math.floor(saved.event.ts/brainMinute)*brainMinute : saved.orders.length>0) ||
        saved.orders.some(o=>typeof o.id!=='string' || ![1,-1].includes(o.side) ||
          !['PENDING','OPEN','CANCELLING'].includes(o.status) || ![o.entry,o.stop,o.risk].every(number))) return null;
    return saved;
  } catch { return null; }
}

function saveBrain() {
  try {
    const key=brainStorageKey(state.instrument);
    globalThis.localStorage?.removeItem(key.replace('bzu_live_trade_state:','bzu-paper-v14.2:'));
    if (!brain.event && !brain.used.size) {
      globalThis.localStorage?.removeItem(key);
      return;
    }
    globalThis.localStorage?.setItem(key,JSON.stringify({instrument:state.instrument,
      orders:brain.event?brain.orders:[], event:brain.event, canonicalIds:brain.canonicalIds??[],
      used:[...brain.used].slice(-128)}));
  } catch { /* The current tab retains its paper state if storage is disabled. */ }
}

function syncBrain(snapshot, first) {
  if (!liveBrainEnabled()) return;
  const covered = snapshot.execution_audit?.closed_through_ts ??
    Math.floor((snapshot.ledger_ts ?? snapshot.snapshot_ts)/brainMinute)*brainMinute;
  if (first || brain.instrument!==snapshot.instrument) {
    const saved=loadBrain(snapshot.instrument);
    Object.assign(brain,{instrument:snapshot.instrument,series:{},rows:[],liquidity:[],used:new Set(saved?.used),computed:0,
      lastTs:saved?.event?.ts??covered,event:saved?.event??null,orders:saved?.orders??[],
      canonicalIds:saved?.canonicalIds??[]});
  }
  for (const event of snapshot.recent_events ?? []) {
    if (['CANCELLED','EXPIRED','CLOSED'].includes(event.kind)) brain.used.add(event.order_id);
  }
  const canonical = snapshot.orders;
  const current = brain.orders;
  const previous = brain.canonicalIds ?? [];
  const observed = brain.event;
  const newLedgerOrder = canonical.some(o=>!previous.includes(o.id) &&
    (!observed || o.status==='OPEN' || (o.placed_ts??0)>=observed.ts || covered>observed.ts));
  const conflictingOrder=observed && covered>observed.ts && canonical.some(o=>
    current.length && current.every(local=>local.id!==o.id) && (o.status==='OPEN'||o.status==='PENDING'));
  const coversProjection = observed && observed.ts < covered;
  const browserOnly = current.length && current.every(o=>o.browser_created);
  const ledgerProgress = canonical.some(o=>o.status==='OPEN' &&
    (observed?.kind==='ENTERED' || (observed?.kind==='TP1' && o.tp1_partial)));
  const ledgerFinished = observed && (snapshot.recent_events??[]).some(e=>e.order_id===observed.orderId &&
    ['CLOSED','CANCELLED','EXPIRED'].includes(e.kind) && e.ts >= observed.ts);
  if (!observed || newLedgerOrder || conflictingOrder || ledgerProgress || ledgerFinished ||
      (coversProjection && !browserOnly && !observed.browserCreated)) {
    brain.orders = canonical.map(o=>({...o}));
    brain.lastTs = Math.max(brain.lastTs,covered);
    brain.event = null;
  }
  if (canonical.some(o=>o.status==='CANCELLING')) {
    brain.orders=canonical.map(o=>({...o}));
    brain.event=null;
  }
  brain.canonicalIds = canonical.map(o=>o.id);
  brain.sourceTs = snapshot.snapshot_ts;
  saveBrain();
}

function brainEvent(order, kind, ts, message) {
  brain.event={kind,ts,candleTs:Math.floor(ts/brainMinute)*brainMinute,side:order.side,
    orderId:order.id,browserCreated:order.browser_created===true,detail:message};
  order.local = true;
  saveBrain();
  notify(order,orderTracker(order),'BROWSER_'+kind,message+' · PAPER');
}

function advanceBrain(market, matrix, rows) {
  if (market.ts < brain.lastTs || market.ts === brain.lastTs && (brain.orders.length || brain.event?.ts===market.ts) || !number(market.bid) || !number(market.ask) ||
      market.bid <= 0 || market.ask < market.bid) return;
  const config = state.brain_config;
  brain.lastTs = market.ts;
  const order=brain.orders[0];
  const oppositeQuote = side => side === 1 ? market.bid : market.ask;
  const eligible = rows.filter(z=>z.activation_ready!==false && tacticalCandidate(z,market.price) && !brain.used.has(z.id));
  const choose = (side) => eligible.find(z=>(!side || side===z.side) && z.market_now) ??
    eligible.find(z=>!side || side===z.side);
  let next = choose();
  const remove = (kind,message) => {
    brain.used.add(order.id);
    brain.orders=[];
    brainEvent(order,kind,market.ts,message);
  };
  if (order?.status === 'CANCELLING' || brain.orders.length>1) return;
  if (order?.status === 'PENDING') {
    const side = order.side, move = side*(market.price-order.entry);
    const target = Math.min(config.missed_target_r*order.risk,
      number(order.tp1) ? side*(order.tp1-order.entry) : Infinity);
    const closer = eligible.filter(z=>z.side===side && z.known_ts>(order.placed_ts ?? order.effective_ts ?? 0) &&
      Math.abs((z.market_now ? z.reference : z.entry)-market.price)<move && z.id!==order.id);
    const replacement = matrix?.side===side && matrix.atr>0 && move>config.browser_replace_atr*matrix.atr &&
      (closer.find(z=>z.market_now) ?? closer[0]);
    if (market.ts >= order.expires_ts) remove('EXPIRED','Строк ліміту минув');
    else if (market.ts >= (order.effective_ts ?? 0) &&
        side*((side===1?market.ask:market.bid)-order.entry)<=1e-8) {
      Object.assign(order,{status:'OPEN',filled_ts:market.ts});
      brainEvent(order,'ENTERED',market.ts,'Вхід виконано '+format(order.entry));
    } else if (move>=target-1e-8 && order.target_cancel_armed!==false) {
      remove('CANCELLED','Рух досяг 0.75R або TP1 без входу');
      next = choose(side);
    } else if (replacement) {
      remove('CANCELLED','Ближчий підтверджений сетап');
      next = replacement;
    } else if (move<target-1e-8) order.target_cancel_armed=true;
  }
  if (order?.status === 'OPEN' && brain.orders[0] === order) {
    const side=order.side, usedQuote=oppositeQuote(side);
    if (side*(usedQuote-order.stop)<=1e-8) {
      const realized=(order.remaining??1)*side*(usedQuote-order.entry)/order.risk+(order.paper_realized_r??0);
      remove('SL','Захисний стоп · '+signed(realized)+'R');
      return;
    }
    const scale = order.scale_policy==='SCALE_OUT_1R';
    const first = scale ? order.entry+side*order.risk : order.tp1;
    if (!order.tp1_hit && number(first) && side*(usedQuote-first)>=-1e-8) {
      const fraction=scale ? .5 : order.tp1_fraction ?? 1;
      order.paper_realized_r=fraction*side*(first-order.entry)/order.risk;
      if (fraction===1) {
        remove('TP1','Вихід на першій цілі');
        return;
      }
      Object.assign(order,{tp1: first,tp1_hit:true,tp1_partial:true,remaining:1-fraction});
      if (scale) {
        const tick=state.tick??.01;
        const raw=(order.entry+side*.15*order.risk)/tick;
        order.stop=(side===1?Math.ceil(raw-1e-8):Math.floor(raw+1e-8))*tick;
        order.be_stop=order.stop;
        order.be_confirmed_ts=market.ts;
      }
      brainEvent(order,'TP1',market.ts,'Зафіксовано '+Math.round(fraction*100)+'% на '+format(first));
    }
    if (order.tp1_hit && number(order.tp2) && side*(usedQuote-order.tp2)>=-1e-8)
      remove('TP2','Вихід залишку на TP2');
    return;
  }
  if (!brain.orders.length && next && !config.uncertain && brainSessionAllowed(market.ts)) {
    const marketEntry=next.order_type==='MARKET';
    const order={...next, browser_created:true, status:marketEntry?'OPEN':'PENDING',
      qualification_tp1:next.tp1,tp1:next.entry+next.side*next.risk,initial_stop:next.stop,placed_ts:market.ts,
      effective_ts:market.ts,expires_ts:Math.min(market.ts+config.ttl_ms,
        Math.floor(market.ts/86_400_000)*86_400_000+22*3_600_000),
      scale_policy:'SCALE_OUT_1R',tp1_fraction:.5,remaining:1,target_cancel_armed:true,
      filled_ts:marketEntry?market.ts:null};
    brain.used.add(order.id);
    brain.orders=[order];
    brainEvent(order,marketEntry?'ENTERED':'PLACED',market.ts,
      (marketEntry?'MARKET · вхід виконано ':'Ліміт виставлено ')+format(order.entry));
  }
}

function recalculateBrain(market) {
  if (!liveBrainEnabled() || !market || Math.abs(Date.now()-market.ts)>QUOTE_MAX_AGE || document.hidden) return;
  const rows=(brain.series['15m']??[]).filter(b=>b.ts+brainQuarter<=market.ts);
  const minutes=(brain.series['1m']??[]).filter(b=>b.ts+brainMinute<=market.ts);
  const m5=(brain.series['5m']??[]).filter(b=>b.ts+5*brainMinute<=market.ts);
  const matrix=brainIndicators(rows,minutes,market);
  brain.matrix=matrix;
  brain.liquidity=matrix ? brainPivots(rows.slice(-96),2,brainQuarter).filter(point=>!rows.some(b=>
    b.ts>=point.known_ts && point.side*(b.close-point.price)<0)) : [];
  brain.rows=matrix ? scanLiveCandlesInBrowser(rows,m5,market,matrix,state.brain_config,state.tick??.01,minutes) : [];
  brain.computed=matrix ? Date.now() : 0;
  advanceBrain(market,matrix,brain.rows);
}

let brainBusy = false;
async function pollBrainCandles() {
  if (!liveBrainEnabled() || brainBusy || document.hidden) return;
  brainBusy=true;
  const instrument=state.instrument, now=Date.now();
  try {
    const timeframes=[['1m',brainMinute,30],['5m',5*brainMinute,64],['15m',brainQuarter,192]];
    await Promise.allSettled(timeframes.map(async ([bar,step,limit])=>{
      if (bar==='1m' && brain.series[bar]?.at(-1)?.ts+step === Math.floor(now/step)*step) return;
      const data=await json(OKX+'/candles?'+new URLSearchParams({instId:instrument,bar,limit}),2700);
      if (state.instrument!==instrument || data.code!=='0' || !Array.isArray(data.data)) return;
      const fresh=brainCandles(data.data,step,Date.now());
      if (fresh.length && fresh.at(-1).ts>=(brain.series[bar]?.at(-1)?.ts??0)) brain.series[bar]=fresh;
    }));
    recalculateBrain(quote);
    render();
  } finally { brainBusy=false; }
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
           EMA9_MOMENTUM_SURF:'Продовження від EMA9', V_REVERSAL_CHOCH:'Розворот із пробоєм шиї',
           FAILED_BREAKOUT_TRAP:'Хибний пробій із поверненням', TURTLE_SOUP:'Turtle Soup · хибний пробій',
           MULTIBAR_SWEEP:'Свіп із поглинанням', BREAKDOWN_RECLAIM:'Ведмежий пробій підтримки', SR_FLIP_RECLAIM:'Дзеркальний рівень', RECLAIM_BREAKER:'Ретест повернутого рівня',
           CONTINUATION_PULLBACK:'Відкат поточної хвилі', M5_IMPULSE_OB:'Імпульсна зона M5',
           CONSOLIDATION_BREAKOUT_BASE:'Наторговка після імпульсу',
           FLAG_RETEST:'Ретест пробою прапора', BEARISH_BREAKER:'Дзеркальний опір',
           BULLISH_BREAKER:'Дзеркальна підтримка', MOMENTUM_STAIRCASE_15M:'Продовження тренду'})[kind] ?? 'Структурна зона';
}
function pendingTargetReached(order, price) {
  if (orderTracker(order).touch) return false;
  if (liveBrainEnabled() && brain.sourceTs === state.snapshot_ts) return false;
  if (!number(order.missed_target_level)) return targetExhausted(order, price);
  return order.target_cancel_armed !== false &&
    (exhaustedPlans.has(exhaustionKey(order)) ||
      number(price) && order.side * (price - order.missed_target_level) >= -1e-8);
}
function pendingInvalidation(order, price) {
  if (number(order.expires_ts) && order.expires_ts <= Date.now())
    return 'Строк лімітки минув. Чекаємо оновлення журналу.';
  if (pendingTargetReached(order, price))
    return 'Рух досяг порога скасування. Чекаємо звірки виконання за журналом.';
  if (!number(order.entry) || (!liveBrainEnabled() && Math.abs(price - order.entry) > maximumDistance(order) + 1e-8))
    return 'Рівень лімітки вийшов за робочу відстань. Чекаємо оновлення журналу.';
  return null;
}
function pendingIsCurrent(order, price) {
  return !pendingInvalidation(order, price);
}
function positionBox(order, price=displayedPrice()) {
  if (!order || !(order.status==='OPEN' || order.status==='PENDING'&&pendingIsCurrent(order,price)) ||
      ![order.entry,order.stop,order.tp1,order.risk].every(number) || order.risk<=0) return null;
  const zone=order.zone?.low ? order.zone : order;
  const target=number(order.tp2)&&order.side*(order.tp2-order.tp1)>0 ? order.tp2 : order.tp1;
  const origin=order.filled_ts || order.placed_ts || order.effective_ts;
  const zoneStep=({'5m':5*brainMinute,'15m':brainQuarter,'1H':60*brainMinute,'4H':240*brainMinute})[zone.timeframe];
  const zoneStart=zone.origin_ts??(number(zone.known_ts)&&zoneStep?zone.known_ts-zoneStep:null);
  const drawZone=number(zone.low)&&number(zone.high)&&zone.high>zone.low&&number(zoneStart);
  const riskR=order.side*(order.stop-order.entry)/order.risk;
  const zoneLabel='POI '+(zone.timeframe??'')+' '+zoneName(zone.kind??order.setup);
  return {
    entry:order.entry, origin, pending:order.status==='PENDING', target, riskR,
    stop:order.stop, tp1:order.tp1,
    poi:drawZone?{low:zone.low,high:zone.high,start:zoneStart,
      label:zoneLabel+' ['+format(zone.low)+'–'+format(zone.high)+']'}:null,
    reward:order.side*(target-order.entry)/order.risk,
    liquidity:{...brainLiquidity(brain.liquidity??[],order.entry),...order.liquidity},
  };
}

function drawPositionBox(target, box, levels) {
  if (!box || !history.length) return;
  const scale=chart.timeScale(), step=chartFrames[frame], latest=currentBar??history.at(-1);
  const timeX=ts=>{
    if (!number(ts)) return null;
    const wanted=Math.floor(ts/(step*1000))*step;
    if (wanted<=history[0].time) return scale.timeToCoordinate(history[0].time);
    const bar=wanted>=latest.time ? latest : history.find(row=>row.time>=wanted);
    return bar ? scale.timeToCoordinate(bar.time) : null;
  };
  const at=timeX(box.origin), current=scale.timeToCoordinate(latest.time);
  if (at===null || current===null) return;
  target.useMediaCoordinateSpace(({context,mediaSize})=>{
    const {width,height}=mediaSize;
    const right=Math.min(width,Math.max(at+38,current+8));
    const drawRect=(left,end,priceA,priceB,color,label)=>{
      const y1=series.priceToCoordinate(priceA), y2=series.priceToCoordinate(priceB);
      if (left===null || y1===null || y2===null) return;
      const x=Math.max(0,left), w=Math.min(width,end)-x;
      const top=Math.max(0,Math.min(y1,y2)), bottom=Math.min(height,Math.max(y1,y2));
      if (w<=0 || bottom<=top) return;
      context.fillStyle=color;
      context.fillRect(x,top,w,bottom-top);
      if (label && bottom-top>=13 && w>27) {
        context.fillStyle='#e1e8f1';
        context.fillText(label,x+5,top+Math.min((bottom-top+8)/2,bottom-top-2),w-10);
      }
    };
    context.save();
    context.font='11px system-ui';
    if (box.poi) drawRect(timeX(box.poi.start),Math.min(width,current+8),box.poi.low,box.poi.high,
      'rgba(167,176,191,.24)',box.poi.label);
    if (box.riskR<0) drawRect(at,right,box.entry,box.stop,'rgba(244,102,125,.14)');
    drawRect(at,right,box.entry,box.target,'rgba(47,202,179,.14)');
    drawChartRays(context,levels,at,width,height);
    const y=series.priceToCoordinate(box.entry);
    if (at>=0&&at<width&&y!==null&&y>=0&&y<height) {
      context.beginPath();
      context.arc(at,y,4,0,2*Math.PI);
      if (box.pending) {
        context.strokeStyle='#88ffe3';
        context.lineWidth=2;
        context.stroke();
      } else {
        context.fillStyle='#88ffe3';
        context.fill();
      }
    }
    context.restore();
  });
}

function drawChartRays(context, levels, origin, width, height) {
  const left=Math.max(0,origin);
  if (left>=width) return;
  const visible=levels.map(level=>({...level,y:series.priceToCoordinate(level.price)}))
    .filter(level=>number(level.y)&&level.y>=0&&level.y<=height).sort((a,b)=>a.y-b.y);
  const flagHeight=14, gap=flagHeight+2;
  const flags=width>=80 ? visible.slice(0,Math.floor(height/gap)) : [];
  let previous=flagHeight/2-gap;
  for (const [index,flag] of flags.entries()) {
    const limit=height-flagHeight/2-(flags.length-index-1)*gap;
    flag.center=Math.min(limit,Math.max(flag.y,previous+gap));
    previous=flag.center;
  }

  context.lineWidth=1;
  for (const level of visible) {
    const y=Math.floor(level.y)+.5;
    context.beginPath();
    context.strokeStyle=level.color;
    context.setLineDash(level.label==='ENTRY'||level.label==='LIMIT'?[8,3,2,3]:[3,4]);
    context.moveTo(left,y);
    context.lineTo(width,y);
    context.stroke();
  }
  context.setLineDash([]);
  context.textBaseline='middle';
  context.font='600 10px system-ui';
  for (const flag of flags) {
    const flagWidth=context.measureText(flag.label).width+10;
    const x=width-2-flagWidth;
    if (Math.abs(flag.center-flag.y)>1) {
      context.beginPath();
      context.strokeStyle=flag.color;
      context.moveTo(x-7,flag.y);
      context.lineTo(x,flag.center);
      context.stroke();
    }
    context.fillStyle='rgba(11,16,24,.88)';
    context.fillRect(x,flag.center-flagHeight/2,flagWidth,flagHeight);
    context.fillStyle=flag.color;
    context.fillText(flag.label,x+5,flag.center);
  }
}

function displayedPrice() {
  const now = Date.now();
  return quote && quote.ts <= now + 3000 && now - quote.ts <= QUOTE_MAX_AGE ? quote.price : state.price;
}
function tacticalCandidate(row, price) {
  if (!row || !row.actionable || row.gate_reason || row.risk_gate || ![1,-1].includes(row.side) ||
      !number(row.score) || row.score < 60 || row.score > 100 ||
      ![row.entry,row.stop,row.tp1,row.tp2].every(p=>number(p)&&p>0) ||
      (number(row.valid_until) && row.valid_until<=Date.now())) return false;
  const risk=row.side*(row.entry-row.stop);
  return risk>0 && row.side*(row.tp1-row.entry)>=1.35*risk-1e-8 &&
    row.side*(row.tp2-row.tp1)>-1e-8 &&
    row.side*(price-row.stop)>0 && !targetExhausted(row,price);
}

function tacticalRadar(price=displayedPrice()) {
  const range=['RANGE','SIDEWAYS','RANGE_LIQUIDITY_ROTATION'].includes(state.market_regime);
  const bias=liveBrainFresh()?brain.matrix?.side:range?0:state.primary_bias;
  const orders=displayOrders();
  const active=orders.find(o=>o.status==='OPEN');
  const pending=orders.find(o=>o.status==='PENDING'&&pendingIsCurrent(o,price));
  const order=active??pending??orders.find(o=>o.status==='CANCELLING'||o.status==='PENDING');
  const candidates=[];
  const source=liveBrainFresh()?brain.rows:ready(Date.now())?poolRows():[];
  const ranked=source.filter(row=>tacticalCandidate(row,price) && (!row.local||!brain.used.has(row.id)) &&
    (!bias||row.side===bias) &&
    !orders.some(o=>row.order_id===o.id || o.side===row.side&&Math.abs(o.entry-row.entry)<(state.tick??.01)))
    .sort((a,b)=>Math.abs(a.entry-price)-Math.abs(b.entry-price)||b.score-a.score);
  for (const row of ranked) {
    if (!candidates.some(other=>other.id===row.id || other.side===row.side&&
        Math.abs(other.entry-row.entry)<(state.tick??.01)*2)) candidates.push(row);
  }
  const plan=candidates[0];
  const direction=order?.side??plan?.side??0;
  const level=plan?.activation_level??plan?.entry;
  const focus=order?(active?'В УГОДІ ':pending?'ЛІМІТ ':'ЗВІРКА ')+sideLabel(direction)+' @ '+format(order.entry):
    plan?'ФОКУС РАДАРА · ОЧІКУЄМО '+sideLabel(direction)+' ВІД '+format(level)+' $ (Δ '+signed(level-price,' $')+')':
      'ФОКУС РАДАРА · ШУКАЄМО СЕТАП';
  return {order,plan,queue:candidates.slice(0,order?3:4),direction,focus};
}

function activationText(plan,price) {
  const level=plan.activation_level??plan.entry;
  if (plan.activation_ready===true || plan.market_now) return 'Умова доступна · перевіряємо котирування';
  if (plan.market_waiting_rr) return 'Ціна в зоні · чекаємо котирування з R:R від 1.35';
  if (plan.range_blocked && Math.abs(level-price)<=(plan.market_radius??0))
    return 'Ціна в середині діапазону · чекаємо виходу або підтвердженого пробою';
  if (plan.activation_kind==='BREAKOUT')
    return 'Чекаємо підтвердженого пробою '+format(level)+' $ і входу в робочий радіус';
  if (Math.abs(level-price)<=(plan.market_radius??state.tick??.01))
    return 'Ціна в зоні · чекаємо підтвердження за свічкою або придатного bid/ask';
  const approach='Чекаємо підходу до '+format(level)+' $ (Δ '+signed(level-price,' $')+')';
  if (!number(plan.limit_radius)) return approach;
  const edge=level+plan.side*plan.limit_radius;
  return approach+' · LIMIT від '+(plan.side===1?'ask ≤ ':'bid ≥ ')+format(edge)+' $';
}

function grossPnlR(plan, price) {
  if (!number(price) || !number(plan.entry) || !number(plan.risk) || plan.risk<=0) return null;
  const initial=plan.paper_realized_r ??
    (plan.tp1_partial ? (plan.tp1_fraction??.5)*plan.side*(plan.tp1-plan.entry)/plan.risk : 0);
  return initial+(plan.remaining??1)*plan.side*(price-plan.entry)/plan.risk;
}

function renderPositionDistance(order, price) {
  const targetKey=order.tp1_partial?'TP2':'TP1';
  const target=order[targetKey.toLowerCase()];
  const {side,stop,entry}=order;
  const box=element('div',null,'telemetry-distance');
  const labels=element('div',null,'rr-endpoints');
  const distance=value=>number(price)&&number(value)?format(Math.max(0,value))+' $':'—';
  const toStop=number(stop)?side*(price-stop):null;
  const toTarget=number(target)?side*(target-price):null;
  labels.append(element('span','До SL · '+distance(toStop),'rr-stop'),
    element('span','До '+targetKey+' · '+distance(toTarget),'rr-target'));
  box.append(labels);
  const span=side*(target-stop);
  if (![price,target,stop].every(number) || span<=0) return box;
  const percent=value=>side*(value-stop)/span*100;
  const current=Math.min(100,Math.max(0,percent(price)));
  const entryPercent=percent(entry);
  const track=element('div',null,'rr-progress-track');
  track.setAttribute('role','meter');
  track.setAttribute('aria-label','Положення ціни від SL до '+targetKey);
  track.setAttribute('aria-valuemin','0');
  track.setAttribute('aria-valuemax','100');
  track.setAttribute('aria-valuenow',String(Math.round(current)));
  if (number(entry) && entryPercent>=0 && entryPercent<=100) {
    const marker=element('span',null,'rr-entry-marker');
    marker.title='Точка входу · '+format(entry);
    marker.style.left=entryPercent+'%';
    track.append(marker);
  }
  const cursor=element('span',null,'rr-price-marker');
  cursor.title='Ціна · '+format(price);
  cursor.style.left=current+'%';
  track.append(cursor);
  box.append(track);
  return box;
}

function renderScaleOut(order) {
  const completed=order.tp1_partial;
  const protectedStop=order.side*(order.stop-order.entry)>=0;
  const fraction=order.tp1_fraction??.5;
  const share=new Intl.NumberFormat('uk-UA',{style:'percent',maximumFractionDigits:2}).format(fraction);
  const text=completed
    ? 'Зафіксовано '+share+(protectedStop?' · Б/У '+format(order.stop):' · чекаємо захисного стопа')
    : order.scale_policy==='SCALE_OUT_1R'?'Scale-Out +1.0R · очікуємо фіксації '+share:'Очікуємо TP1';
  const chip=element('p',null,'scale-checkpoint');
  chip.dataset.complete=String(Boolean(completed));
  const icon=element('span',completed?'✓':'◷','scale-icon');
  icon.setAttribute('aria-hidden','true');
  chip.append(icon,element('span',text));
  if (order.local) chip.append(element('span','PAPER','scale-paper'));
  return chip;
}

function renderTacticalRadar(price, view, pnl, tone) {
  const {order,queue,direction,focus}=view;
  const header=$('tactical-focus');
  header.textContent=focus;
  header.dataset.side=direction===1?'long':direction===-1?'short':'range';
  header.dataset.pnlTone=tone;
  $('radar-source').textContent=liveBrainFresh()?'REAL-TIME · 3 С':'ЗА ЖУРНАЛОМ';
  const primary=$('radar-primary');
  primary.hidden=!order;
  primary.dataset.pnlTone=tone;
  primary.replaceChildren();
  if (order) {
    const active=order.status==='OPEN';
    const kind=order.setup??order.kind;
    const heading=element('div',null,'bar');
    heading.append(element('h3',(order.timeframe??order.zone?.timeframe??'')+' '+zoneName(kind)+' · '+sideLabel(order.side)),
      element('span',active?'В УГОДІ · '+(order.order_type??order.type??'LIMIT'):
        order.status==='CANCELLING'?'ЗВІРКА':'ЛІМІТНИЙ ОРДЕР','badge'));
    const metrics=element('dl',null,'tactical-metrics');
    for (const [key,label,value] of [['entry','Вхід',format(order.entry)],['stop','Стоп',format(order.stop)],
      ['tp1','Тейк 1',format(order.tp1)+' · '+signed(order.side*(order.tp1-order.entry)/order.risk)+'R'],
      ['tp2','Тейк 2',format(order.tp2)]]) {
      const metric=element('div');
      metric.dataset.level=key;
      metric.append(element('dt',label),element('dd',value));
      metrics.append(metric);
    }
    primary.append(heading,metrics);
    if (active) {
      const panel=element('div',null,'position-telemetry');
      const summary=element('div',null,'telemetry-pnl');
      const dot=element('span',null,'pnl-dot');
      dot.setAttribute('aria-hidden','true');
      summary.append(dot,element('span','Поточний PnL','telemetry-label'),
        element('strong',signed(pnl,'R'),'pnl-badge'),
        element('span',signed(number(pnl)?pnl*order.risk:null,' $/од.'),'pnl-unit'));
      panel.append(summary,renderPositionDistance(order,price));
      primary.append(panel);
    } else {
      primary.append(element('p','До входу '+(number(price)?format(Math.abs(price-order.entry))+' $':'—'),'tactical-distance'));
    }
    primary.append(renderScaleOut(order),copyButtons(order));
  }
  $('queue-title').textContent=order?'Резервні сетапи':'Черга сетапів на радарі · Спостереження';
  $('tactical-reserves').hidden=!queue.length;
  $('zones').replaceChildren(...queue.map(plan=>{
    const row=element('tr');
    const side=element('strong',sideLabel(plan.side)+' · '+(plan.timeframe??''),plan.side===1?'long':'short');
    const name=td('');
    name.append(side,element('small',zoneName(plan.kind??plan.setup)));
    const risk=plan.side*(plan.entry-plan.stop);
    row.append(name,td(format(plan.entry)),td(format(plan.stop)),td(format(plan.tp1)+' · '+
      (plan.side*(plan.tp1-plan.entry)/risk).toFixed(2)+'R'),td(activationText(plan,price)));
    return row;
  }));
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
    item.dataset.eventTone=({FILLED:'entry',PLACED:'order',CANCELLED:'inactive',EXPIRED:'inactive'})[e.kind]??'neutral';
    item.append(element('time',eventTime(e.ts)),element('span',eventText(e)));
    return item;
  }));
}
function setBannerMode(mode) {
  const banner = $('state-banner');
  banner.dataset.mode = mode;
  for (const name of ['waiting','pending','entered','managing','closed']) {
    banner.classList.toggle('banner-' + name, mode === name.toUpperCase());
  }
  return banner;
}
function renderBanner(price, usable, view, pnl, tone) {
  const {order,plan}=view;
  let mode='WAITING', pill='РАДАР', title='РАДАР · СКАНУЄМО РИНОК', detail='Чекаємо підтвердженого сетапу.';
  if (order?.status==='OPEN') {
    mode=order.tp1_partial?'MANAGING':'ENTERED';
    pill=(order.local?'PAPER · ':'В УГОДІ · ')+(order.order_type??order.type??'LIMIT');
    title='В УГОДІ '+sideLabel(order.side)+' @ '+format(order.entry);
    const pnlText=number(pnl)?signed(pnl,'R')+' ('+signed(pnl*order.risk,' $/од.')+')':'—';
    detail='PnL '+pnlText+' · SL '+format(order.stop)+' | TP1 '+format(order.tp1);
  } else if (order?.status==='CANCELLING'||(order?.status==='PENDING'&&!pendingIsCurrent(order,price))) {
    pill='ЗВІРКА';
    title='ЗВІРКА ЛІМІТКИ '+sideLabel(order.side)+' @ '+format(order.entry);
    detail=pendingInvalidation(order,price)??'Очікуємо підтвердження скасування.';
  } else if (order?.status==='PENDING') {
    mode='PENDING';
    pill=order.local?'PAPER · LIMIT':'LIMIT';
    title='ЛІМІТ ВИСТАВЛЕНО '+sideLabel(order.side)+' @ '+format(order.entry);
    detail='Δ '+format(Math.abs(price-order.entry))+' $ · SL '+format(order.stop)+' | TP1 '+format(order.tp1);
  } else if (plan) {
    const level=plan.activation_level??plan.entry;
    title='РАДАР В ОЧІКУВАННІ · Найближчий сетап '+sideLabel(plan.side)+' ['+
      zoneName(plan.kind??plan.setup)+'] @ '+format(level)+' $';
    detail='До активації '+signed(level-price,' $')+' · '+activationText(plan,price);
  } else if (!usable && !liveBrainFresh()) {
    title='ЧЕКАЄМО ОНОВЛЕННЯ ДАНИХ';
    detail='Очікуємо котирування й закриті свічки OKX.';
  } else if (liveBrainEnabled() && ['SL','TP1','TP2'].includes(brain.event?.kind) &&
      Date.now()-brain.event.ts<15*brainMinute) {
    mode='CLOSED';
    title='УГОДУ ЗАКРИТО · PAPER';
    detail=brain.event.detail;
  }
  const banner=setBannerMode(mode);
  banner.dataset.pnlTone=tone;
  banner.dataset.positionState=order?.status==='OPEN'?'ENTERED':mode==='PENDING'?'LIMIT_PLACED':
    order?'RECONCILING':'FLAT';
  $('state-title').textContent=title+' · '+detail;
  $('state-detail').textContent=detail;
  $('state-pill').textContent=pill;
}

function render() {
  if (!state) return;
  const focusId=document.activeElement?.dataset.focus;
  const now=Date.now(), live=quote && quote.ts<=now+3000 && now-quote.ts<=QUOTE_MAX_AGE;
  const price=displayedPrice(), usable=ready(now);
  const market=usable && live && !document.hidden ? quote : null;
  $('price').textContent=format(price);
  $('quote-status').textContent=live ? 'Live · ' + stamp(quote.ts) : (feedError ? 'OKX недоступний' : 'Котирування застаріло') + ' · зліпок';
  $('stamp').textContent=liveBrainFresh(now)?stamp(brain.computed):'—'; $('instrument').textContent=state.instrument;
  $('regime').textContent=(usable ? state.scenario_plan?.market?.phase_text : null) ?? ({BULLISH_PULLBACK_OPPORTUNITY:'Висхідний · відкат',
    BEARISH_PULLBACK_OPPORTUNITY:'Низхідний · відкат'})[state.trend_phase] ?? ({BULLISH_ACTIVE:'Висхідний',BEARISH_ACTIVE:'Низхідний',RANGE:'Діапазон',PULLBACK_PHASE:'Відкат'})[state.market_regime] ?? 'Невідомий';
  if (liveBrainFresh(now) && brain.matrix) $('regime').textContent=
    ({1:'Висхідний',[-1]:'Низхідний',0:'Діапазон'})[brain.matrix.side];
  const structureSync=state.structure_sync;
  $('regime').title=structureSync?.proof === 'RECENT_SWING_IMPULSE'
    ? 'Хвиля від останнього підтвердженого свінга · ' + format(structureSync.wave_atr) + ' ATR'
    : structureSync?.proof === 'EMA9_EMA20_SESSION_VWAP'
    ? 'Закриття M15 та EMA9 відносно EMA20 · напрямок підтверджує VWAP'
    : structureSync?.proof === 'BOS_IMPULSE_BREAKOUT'
    ? 'Закритий пробій структури · EMA20, VWAP та обсяг'
    : structureSync?.proof === 'BEARISH_BREAKDOWN_MSS'
    ? 'Закритий ведмежий пробій M15'
    : structureSync?.proof === 'DISPLACEMENT_MSS_FVG'
    ? 'Свіп / межа ADR · MSS M15 · FVG'
    : structureSync?.proof === 'M15_BOS_EMA20_ATR'
    ? 'Злам M15 ' + format(structureSync.level) + ' · закриття за EMA20 · ' + format(structureSync.wave_atr) + ' ATR'
    : structureSync?.proof === 'M15_BOS_75_ADR'
    ? 'Пробій M15 ' + format(structureSync.level) + ' · хвиля ' + Math.round(structureSync.wave_adr * 100) + '% ADR'
    : '';
  $('state-status').textContent='Очікуємо свіжі дані OKX';
  if (liveBrainFresh(now)) {
    $('state-status').textContent='Real-Time 3s Active';
    $('regime').title='EMA9 '+format(brain.matrix.liveEma9??brain.matrix.ema9)+' · EMA20 '+format(brain.matrix.liveEma20??brain.matrix.ema20)+
      ' · EMA50 '+format(brain.matrix.liveEma50??brain.matrix.ema50)+' · VWAP '+format(brain.matrix.vwap)+' · ATR14 '+format(brain.matrix.atr);
  }
  const orders=displayOrders();
  $('capacity').textContent=(liveBrainEnabled()?Math.max(orders.length,state.brain_config?.uncertain??0):(state.occupied_slots ?? state.orders.length)) + '/' + (state.max_new_pending ?? 2) + ' місць';
  $('daily').textContent=state.daily ? state.daily.entries + ' уг. · ' + signed(state.daily.net_r) + 'R' : '—';
  if (!liveBrainEnabled() && usable && !document.hidden) {
    for (const order of orders) track(order,orderTracker(order),market,now);
  }
  const view=tacticalRadar(price);
  const pnl=view.order?.status==='OPEN'?grossPnlR(view.order,price):null;
  const tone=number(pnl)?(pnl<0?'loss':'profit'):'neutral';
  renderBanner(price,usable,view,pnl,tone);
  renderTacticalRadar(price,view,pnl,tone);
  renderActivity();
  drawLevels(view);
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
      const box=chartPosition;
      if (!box) return info;
      const values=[box.entry,box.stop,box.target,box.poi?.low,box.poi?.high,
        ...Object.values(box.liquidity).map(level=>level.price)].filter(number);
      if (values.length) {
        info.priceRange.minValue=Math.min(info.priceRange.minValue,...values);
        info.priceRange.maxValue=Math.max(info.priceRange.maxValue,...values);
      }
      return info;
    }
  });
  const paneView={renderer:()=>({draw:target=>drawPositionBox(target,chartPosition,chartLevels)}),zOrder:()=> 'bottom'};
  series.attachPrimitive({ attached: ({ requestUpdate }) => { chartRequest = requestUpdate; },
    paneViews: () => [paneView] });
}

function drawLevels(view=tacticalRadar()) {
  if (!state) return;
  for (const line of priceLines) series?.removePriceLine(line);
  priceLines=[];
  const specs=[];
  const add=(price,label,description,color) => {
    if (number(price)) specs.push({price,label,description,color});
  };
  const order=view.order, box=positionBox(order);
  chartPosition=box;
  chartLevels=specs;
  $('chart-legend').hidden=!box||!series;
  if (box) {
    const prefix=(order.number?'#'+order.number+' ':'')+sideLabel(order.side)+' ';
    const entryLabel=box.pending?'LIMIT':'ENTRY';
    const protectedStop=box.riskR>=0;
    add(box.entry,entryLabel,prefix+entryLabel,'#c0e0ff');
    add(box.stop,protectedStop?'Б/У':'SL',prefix+(protectedStop?'ЗАХИЩЕНИЙ SL':'SL')+
      ' ('+signed(box.riskR)+'R)',protectedStop?'#f4c76b':'#ff8994');
    add(box.tp1,'TP1',prefix+'TP1 · '+signed(order.side*(box.tp1-box.entry)/order.risk)+'R','#55d6ad');
    if (box.target!==box.tp1) add(box.target,'TP2',prefix+'TP2 · '+signed(box.reward)+'R','#79e4cd');
    for (const [key,above] of [['bsl',true],['ssl',false]]) {
      const level=box.liquidity[key]?.price;
      if (number(level)&&(above?level>box.entry:level<box.entry))
        add(level,key.toUpperCase(),key.toUpperCase(),'#adbacd');
    }
  }
  series?.applyOptions({priceFormat:{type:'price',precision:Math.max(2,(String(state.tick).split('.')[1] ?? '').length),minMove:state.tick || .01}});
  const axisPrices=new Set();
  const legend=[];
  for (const {price,label,description,color} of specs) {
    const axisPrice=format(price);
    if (series) priceLines.push(series.createPriceLine({price,color,lineVisible:false,title:'',
      axisLabelVisible:!axisPrices.has(axisPrice),axisLabelTextColor:'#101925'}));
    axisPrices.add(axisPrice);
    const item=element('span',description+' @ '+axisPrice);
    item.style.color=color;
    legend.push(item);
  }
  $('chart-levels').classList.toggle('sr-only',Boolean(series&&(history.length||currentBar)));
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
  if (quote && price!==quote.price && ts-quote.ts<=QUOTE_MAX_AGE && Date.now()-ts<=QUOTE_MAX_AGE &&
      !document.hidden && !window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) {
    const flash=price>quote.price?'rgba(16,185,129,.25)':'rgba(239,68,68,.25)';
    priceFlash?.cancel();
    priceFlash=$('price').animate?.([{backgroundColor:flash},{backgroundColor:'transparent'}],300);
  }
  quote = {price,ts,bid:positiveNumber(row.bidPx),ask:positiveNumber(row.askPx)};
  recalculateBrain(quote);
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

function bootstrapLiveBrain() {
  if (state) return;
  state={schema:1,cockpit_schema:5,instrument:'BZ-USDT-SWAP',tick:.01,snapshot_ts:0,
    orders:[],zones:[],candles:{},status:'LOCAL_ONLY',price:null,brain_config:{
      schema:1,missed_target_r:.75,browser_replace_atr:1,market_band_atr:.65,
      market_touch_atr:.18,limit_approach_atr:.85,market_min_rr:1.35,limit_min_rr:1.35,
      risk_floor:.65,risk_cap:1.15,ttl_ms:90*brainMinute,uncertain:0}};
  syncBrain(state,true);
}

async function refreshLiveBrain() {
  if (document.hidden) return;
  await Promise.allSettled([pollTicker(),liveBrainEnabled()?pollBrainCandles():pollM5()]);
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
      render();
      return;
    }
    const first = !state || next.instrument !== state.instrument;
    if (first) {
      priceFlash?.cancel();
      priceFlash = undefined;
      quote = undefined; lastSampleTime = 0; history = []; currentBar = undefined;
      if (series) series.setData([]);
      position.clear(); reactions.clear(); m5Proofs.clear(); invalidZones.clear(); exhaustedPlans.clear(); selectedPlanIds.clear(); m5Market=null; previousLiveSample=null;
      disconnectSocket();
    }
    const previousSnapshot = state?.snapshot_ts;
    state = next;
    syncBrain(next, first);
    const knownZones = new Set([...poolRows().map(zoneFingerprint), ...next.orders.map(exhaustionKey)]);
    for (const cache of [invalidZones, exhaustedPlans]) {
      for (const fingerprint of cache) if (!knownZones.has(fingerprint)) cache.delete(fingerprint);
    }
    if (!first) {
      for (const event of next.recent_events ?? []) {
        const tracker = position.get(event.order_id);
        if (tracker && !tracker.events.has('BROWSER_'+event.kind) && ['CANCELLED','EXPIRED'].includes(event.kind) && event.ts >= previousSnapshot) {
          notify(event, tracker, event.kind, (event.kind === 'CANCELLED' ?
            'Бот скасував лімітку ' : 'Час дії лімітки минув ') + format(event.entry));
        }
      }
    }
    const ids = new Set(displayOrders().map(order => order.id));
    for (const id of position.keys()) if (!ids.has(id)) position.delete(id);
    setHistory(next.candles?.[frame] ?? [], first);
    const keep=new Set(reactionRows().map(reactionKey));
    for (const [key,t] of reactions) if (!keep.has(key)&&!['INVALID','MISSED'].includes(t.stage)) reactions.delete(key);
    render();
    if (first) {
      void refreshLiveBrain();
      await loadCandles(true);
    }
  } catch {
    stateError = true;
    $('state-status').textContent = 'Зліпок бота недоступний';
    if (!state) {
      bootstrapLiveBrain();
      void refreshLiveBrain();
      void loadCandles(true);
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
$('radar-primary').addEventListener('click', copyLevel);
initChart();
void pollState();
setInterval(refreshLiveBrain,3000);
setInterval(() => {
  if (state?.brain_config?.worker_interval_ms) void pollState();
},3000);
setInterval(pollState, 30_000);
document.addEventListener('visibilitychange', () => {
  pauseReactions();
  if (!document.hidden) { void pollState(); void refreshLiveBrain(); }
});
