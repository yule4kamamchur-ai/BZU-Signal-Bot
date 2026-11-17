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
  return rows.slice(-14).reduce((total,b,i) => {
    const prev = rows[rows.length-15+i].close;
    return total + Math.max(b.high-b.low, Math.abs(b.high-prev), Math.abs(b.low-prev));
  },0)/14;
}

function brainIndicators(rows, minutes, market) {
  if (rows.length < 50 || rows.at(-1).ts + brainQuarter > market.ts ||
      market.ts - rows.at(-1).ts - brainQuarter >= brainQuarter ||
      rows.some((bar,i) => i && bar.ts - rows[i-1].ts !== brainQuarter)) return null;
  const ema = (period, bars) => bars.slice(1).reduce((value,bar) =>
    value+(bar.close-value)*2/(period+1),bars[0].close);
  const end = rows.at(-1).ts+brainQuarter, midnight = Math.floor(end/86_400_000)*86_400_000;
  const quarters = rows.filter(b => b.ts >= midnight);
  // Use a complete daily prefix. Prefer minute data where the full quarter exists.
  const fine = new Map(minutes.map(b => [b.ts,b]));
  const day = quarters.flatMap(b => {
    const chunk = Array.from({length:15},(_,i)=>fine.get(b.ts+i*brainMinute));
    return chunk.every(Boolean) ? chunk : [b];
  });
  // Add the closed M1 tail of the current M15 without reusing any volume.
  const minuteEnd = Math.floor(market.ts/brainMinute)*brainMinute;
  for (let ts=end; ts<minuteEnd && fine.has(ts); ts+=brainMinute) day.push(fine.get(ts));
  const volume = day.reduce((total,b)=>total+b.volume,0);
  const vwap = quarters[0]?.ts === midnight && volume > 0 ?
    day.reduce((total,b)=>total+(b.high+b.low+b.close)/3*b.volume,0)/volume : null;
  const ema9=ema(9,rows.slice(-120)), ema20=ema(20,rows.slice(-60)), ema50=ema(50,rows.slice(-120));
  const side = [1,-1].find(s => number(vwap) && s*(market.price-ema20)>0 &&
    s*(rows.at(-1).close-ema20)>0 && s*(market.price-vwap)>0 && s*(ema9-ema20)>0) ?? 0;
  const liveEma = (value,period) => value+(market.price-value)*2/(period+1);
  const rangeBars=rows.slice(-25,-1);
  const range={low:Math.min(...rangeBars.map(b=>b.low)),high:Math.max(...rangeBars.map(b=>b.high))};
  return {ema9,ema20,ema50,vwap,atr:brainAtr(rows),side,range,
    liveEma9:liveEma(ema9,9),liveEma20:liveEma(ema20,20),liveEma50:liveEma(ema50,50)};
}

function brainScan(rows, m5, market, matrix, config, tick) {
  if (matrix.atr <= 0) return [];
  const {atr,side:trend} = matrix;
  const result = new Map(), last = rows.at(-1), prior = rows.slice(-13,-1);
  const {low:rangeLow,high:rangeHigh} = matrix.range ?? {};
  const wick = (row,side) => side === 1 ? row.low : row.high;
  const far = (row,side) => side === 1 ? row.high : row.low;
  const roundOut = (price,side) => Math.round((side === 1 ?
    Math.ceil(price/tick-1e-8) : Math.floor(price/tick+1e-8))*tick*1e8)/1e8;
  const quoteFor = side => side === 1 ? market.ask : market.bid;
  const add = (kind, side, ref, anchor, bar, step, trigger) => {
    const known = bar.ts+step;
    if (known > market.ts || market.ts-known >= 3*brainQuarter) return;
    const low = Math.min(ref,anchor), high = Math.max(ref,anchor);
    if (low === high) return;
    const quote = quoteFor(side);
    const structuralEntry = roundOut(ref+side*tick,side);
    const breakout = ['BOS_IMPULSE_BREAKOUT','CONSOLIDATION_BREAKOUT_BASE'].includes(kind);
    const rangePos = number(rangeLow) && rangeHigh > rangeLow ? (quote-rangeLow)/(rangeHigh-rangeLow) : null;
    const insideRange = !trend && (rangePos === null || rangePos >= .30 && rangePos <= .70);
    const edgeBroken = breakout && side*(quote-(side===1?rangeHigh:rangeLow))>tick;
    const closeTouch = number(quote) && Math.abs(quote-structuralEntry) <= (config.market_touch_atr??.20)*atr+1e-8;
    const impulse = breakout && number(quote) && market.ts-known<step &&
      side*(quote-ref) >= tick/2-1e-8 && side*(quote-ref)<=config.market_band_atr*atr+1e-8;
    const marketReady = (!insideRange || edgeBroken) && (closeTouch || trigger && impulse);
    if (trend && trend !== side) return;
    const entry = marketReady ? quote : structuralEntry;
    if (rows.some(b => b.ts >= known && side*(wick(b,side)-anchor) < -tick)) return;
    const riskFloor = config.risk_floor;
    const stop = roundOut(structuralEntry-side*Math.max(riskFloor,side*(structuralEntry-anchor)+Math.max(2*tick,.12*atr)),-side);
    const risk = side*(entry-stop);
    if (risk < riskFloor-1e-8 || risk > config.risk_cap+1e-8) return;
    const pivots = rows.slice(1,-1).filter((b,i) => b.ts < bar.ts &&
      side*(far(b,side)-far(rows[i],side))>0 && side*(far(b,side)-far(rows[i+2],side))>0);
    const levels = [...new Set(pivots.map(b=>far(b,side)))].filter(p=>side*(p-entry)>0)
      .sort((a,b)=>side*(a-b));
    const nearest = levels[0];
    const rr1 = number(nearest) ? side*(nearest-entry)/risk : 0;
    const gap = side*(market.price-entry);
    const orderType = marketReady && rr1 >= (config.market_min_rr??1.35)-1e-8 ? 'MARKET' :
      !marketReady && rr1 >= (config.limit_min_rr??1.40)-1e-8 &&
      gap >= tick && gap < config.missed_target_r*risk ? 'LIMIT' : null;
    if (!orderType) return;
    // Local score reflects a closed structural trigger, intact stop, available room and direction.
    const normalVolume=prior.reduce((total,b)=>total+b.volume,0)/prior.length;
    const score = 25 + 20 + Math.min(25,Math.floor(12*rr1)) + (trend===side ? 15 : 0) +
      (normalVolume>0 && bar.volume>=1.10*normalVolume ? 8 : 0);
    const tp2 = levels.find(p => side*(p-entry)/risk >= Math.max(1.85,rr1+tick/risk)) ??
      roundOut(entry+side*Math.max(1.90,rr1+tick/risk)*risk,side);
    const id = ['paper',kind,side,known,ref.toFixed(4)].join(':');
    result.set(id,{id,kind,setup:kind,side,entry,stop,risk,low,high,reference:ref,score,
      tp1:nearest,tp2,rr1,known_ts:known,timeframe:step===brainQuarter?'15m':'5m',
      order_type:orderType,actionable:!!orderType,market_now:orderType==='MARKET',local:true});
  };
  for (const side of [1,-1]) {
    const edge = f => side*Math.max(...prior.map(b=>side*f(b,side)));
    const breakout=edge(far), sweep=-side*Math.max(...prior.map(b=>-side*wick(b,side)));
    const body=side*(last.close-last.open);
    if ((!trend || side===trend) && side*(last.close-breakout)>tick && body>=.5*atr && last.high-last.low<=2.3*atr) {
      const base=prior.slice(-5), span=Math.max(...base.map(b=>b.high))-Math.min(...base.map(b=>b.low));
      add(span<=1.30*atr?'CONSOLIDATION_BREAKOUT_BASE':'BOS_IMPULSE_BREAKOUT',
        side,breakout,wick(last,side),last,brainQuarter,true);
    }
    if (side*(wick(last,side)-sweep)<-tick && side*(last.close-sweep)>tick && body>=.30*atr)
      add('TURTLE_SOUP',side,sweep,wick(last,side),last,brainQuarter,true);
    const [left,neck,right,confirm] = rows.slice(-4);
    if (side*(wick(right,side)-wick(left,side))>=tick &&
        side*(far(neck,side)-wick(right,side))>=.45*atr &&
        side*(right.close-far(neck,side))<=0 && side*(confirm.close-far(neck,side))>tick &&
        side*(confirm.close-confirm.open)>=.30*atr)
      add('V_REVERSAL_CHOCH',side,far(neck,side),wick(right,side),confirm,brainQuarter,true);
    for (let i=rows.length-3;i<rows.length;i++) {
      const b=rows[i], old=rows[i-2];
      if (side===trend && side*(wick(b,side)-far(old,side))>=.12*atr)
        add(side===1?'FVG_BULL':'FVG_BEAR',side,wick(b,side),far(old,side),b,brainQuarter,false);
    }
    const fresh=m5.at(-1), previous=m5.at(-2), impulse=m5.at(-3), m5step=5*brainMinute;
    if (side===trend && fresh && previous?.ts===fresh.ts-m5step && impulse?.ts===previous.ts-m5step &&
        side*(fresh.close-fresh.open)>tick) {
      if (side*(matrix.ema20-matrix.ema50)>0 && Math.abs(wick(fresh,side)-matrix.ema9)<=.35*atr &&
          side*(fresh.close-matrix.ema9)>tick && side*(impulse.close-impulse.open)>=.30*atr &&
          Math.abs(previous.close-previous.open)<=.35*atr)
        add('EMA9_MOMENTUM_SURF',side,matrix.ema9,wick(fresh,side),fresh,m5step,true);
      const base = m5.slice(-9,-3);
      if (base.length>=5 && base.every((b,i)=>!i||b.ts-base[i-1].ts===m5step)) {
        const level=side*Math.max(...base.map(b=>side*far(b,side)));
        if (side*(impulse.close-level)>tick && side*(previous.close-level)>=0 &&
            Math.abs(wick(fresh,side)-level)<=.38*atr && side*(fresh.close-level)>tick)
          add(side*(impulse.open-level)>=0 ? 'RECLAIM_BREAKER' : 'SR_FLIP_RECLAIM',
            side,level,wick(fresh,side),fresh,m5step,true);
      }
    }
  }
  return [...result.values()].sort((a,b)=>Number(b.actionable)-Number(a.actionable) ||
    Math.abs(a.entry-market.price)-Math.abs(b.entry-market.price));
}

function brainStorageKey(instrument) {
  return 'bzu-paper-v14.2:'+ (globalThis.location?.pathname ?? '/') + ':' + instrument;
}

function loadBrain(instrument) {
  try {
    const saved=JSON.parse(globalThis.localStorage?.getItem(brainStorageKey(instrument)) ?? 'null');
    if (!saved || saved.instrument!==instrument || !Array.isArray(saved.orders) ||
        !Array.isArray(saved.canonicalIds) || !Array.isArray(saved.used) ||
        !saved.event || typeof saved.event.orderId!=='string' || !number(saved.event.ts) ||
        saved.event.ts>Date.now()+5000 || !number(saved.event.candleTs) ||
        saved.event.candleTs!==Math.floor(saved.event.ts/brainMinute)*brainMinute ||
        saved.orders.some(o=>typeof o.id!=='string' || ![1,-1].includes(o.side) ||
          !['PENDING','OPEN','CANCELLING'].includes(o.status) || ![o.entry,o.stop,o.risk].every(number))) return null;
    return saved;
  } catch { return null; }
}

function saveBrain() {
  try {
    const key=brainStorageKey(state.instrument);
    if (!brain.event) {
      globalThis.localStorage?.removeItem(key);
      return;
    }
    globalThis.localStorage?.setItem(key,JSON.stringify({instrument:state.instrument,
      orders:brain.orders, event:brain.event, canonicalIds:brain.canonicalIds??[],
      used:[...brain.used].slice(-128)}));
  } catch { /* The current tab retains its paper state if storage is disabled. */ }
}

function syncBrain(snapshot, first) {
  if (!liveBrainEnabled()) return;
  const covered = snapshot.execution_audit?.closed_through_ts ??
    Math.floor((snapshot.ledger_ts ?? snapshot.snapshot_ts)/brainMinute)*brainMinute;
  if (first || !brain.sourceTs) {
    const saved=loadBrain(snapshot.instrument);
    Object.assign(brain,{series:{},rows:[],used:new Set(saved?.used),computed:0,
      lastTs:saved?.event.ts??covered,event:saved?.event??null,orders:saved?.orders??[],
      canonicalIds:saved?.canonicalIds??[]});
  }
  for (const event of snapshot.recent_events ?? []) {
    if (['CANCELLED','EXPIRED','CLOSED'].includes(event.kind)) brain.used.add(event.order_id);
  }
  const canonical = snapshot.orders;
  const current = brain.orders;
  const previous = brain.canonicalIds ?? [];
  const newLedgerOrder = canonical.some(o=>!previous.includes(o.id));
  const observed = brain.event;
  const coversProjection = observed && observed.ts < covered;
  const browserOnly = current.length && current.every(o=>o.browser_created);
  const ledgerProgress = canonical.some(o=>o.status==='OPEN' &&
    (observed?.kind==='ENTERED' || (observed?.kind==='TP1' && o.tp1_partial)));
  const ledgerFinished = observed && (snapshot.recent_events??[]).some(e=>e.order_id===observed.orderId &&
    ['CLOSED','CANCELLED','EXPIRED'].includes(e.kind) && e.ts >= observed.ts);
  if (!observed || newLedgerOrder || ledgerProgress || ledgerFinished ||
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
  if (market.ts <= brain.lastTs || !number(market.bid) || !number(market.ask) ||
      market.bid <= 0 || market.ask < market.bid) return;
  const config = state.brain_config;
  brain.lastTs = market.ts;
  const order=brain.orders[0];
  const oppositeQuote = side => side === 1 ? market.bid : market.ask;
  const eligible = rows.filter(z=>tacticalCandidate(z,market.price) && !brain.used.has(z.id));
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
      qualification_tp1:next.tp1,tp1:next.entry+next.side*next.risk,placed_ts:market.ts,
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
  brain.rows=matrix ? brainScan(rows,m5,market,matrix,state.brain_config,state.tick??.01) : [];
  brain.computed=matrix ? Date.now() : 0;
  advanceBrain(market,matrix,brain.rows);
}

let brainBusy = false;
async function pollBrainCandles() {
  if (!liveBrainEnabled() || brainBusy || document.hidden) return;
  brainBusy=true;
  const instrument=state.instrument, now=Date.now();
  try {
    const timeframes=[['1m',brainMinute,30],['5m',5*brainMinute,16],['15m',brainQuarter,120]];
    await Promise.allSettled(timeframes.map(async ([bar,step,limit])=>{
      if (brain.series[bar]?.at(-1)?.ts+step === Math.floor(now/step)*step) return;
      const data=await json(OKX+'/candles?'+new URLSearchParams({instId:instrument,bar,limit}),2700);
      if (state.instrument===instrument && data.code==='0' && Array.isArray(data.data))
        brain.series[bar]=brainCandles(data.data,step,Date.now());
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
function chartOrders() {
  const order=tacticalRadar().order;
  return order && (order.status==='OPEN' || order.status==='PENDING'&&pendingIsCurrent(order,displayedPrice())) ? [order] : [];
}
function chartPlan() {
  const view=tacticalRadar();
  return view.order ? null : view.plan??null;
}
function visibleZones() {
  const {plan,order}=tacticalRadar();
  if (order?.status==='CANCELLING'||order?.status==='PENDING'&&!pendingIsCurrent(order,displayedPrice())) return [];
  const zone=order?.zone?.low ? order.zone : plan;
  return zone ? [{...zone,id:plan.id,side:plan.side,timeframe:zone.timeframe??plan.timeframe}] : [];
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
  const side=liveBrainFresh() ? brain.matrix?.side : state.primary_bias;
  const orders=displayOrders();
  const active=orders.find(o=>o.status==='OPEN');
  const pending=orders.find(o=>o.status==='PENDING'&&pendingIsCurrent(o,price));
  const cancelling=orders.find(o=>o.status==='CANCELLING'||o.status==='PENDING');
  const order=active??pending??cancelling;
  const direction=order?.side??side;
  const readyCandidates = [...(liveBrainFresh()?brain.rows:[]), ...(ready(Date.now())?poolRows():[])]
    .filter(row=>tacticalCandidate(row,price) && (!direction || row.side===direction) &&
      !orders.some(o=>row.order_id===o.id || row.side===o.side && Math.abs(row.entry-o.entry)<(state.tick??.01)))
    .sort((a,b)=>Math.abs(a.entry-price)-Math.abs(b.entry-price)||b.score-a.score);
  const candidates=[];
  for (const row of readyCandidates) {
    if (!candidates.some(other=>other.id===row.id || other.side===row.side &&
        Math.abs(other.entry-row.entry)<(state.tick??.01)*2)) candidates.push(row);
  }
  const range=liveBrainFresh() ? brain.matrix?.range : state.dealing_range;
  const focus=direction ? (active?'В УГОДІ ':pending?'ЛІМІТ ':'ОЧІКУЄМО ')+sideLabel(direction) :
    number(range?.low) && number(range?.high) && range.high>range.low ?
      'ДІАПАЗОН · LONG ВІД '+format(range.low)+' | SHORT ВІД '+format(range.high) : 'ДІАПАЗОН · ШУКАЄМО РІВНІ';
  return {order, plan:order??candidates[0], reserves:candidates.slice(order?0:1,order?3:4),
    direction,focus};
}

function grossPnlR(plan, price) {
  const initial=plan.paper_realized_r ??
    (plan.tp1_partial ? (plan.tp1_fraction??.5)*plan.side*(plan.tp1-plan.entry)/plan.risk : 0);
  return initial+(plan.remaining??1)*plan.side*(price-plan.entry)/plan.risk;
}

function renderTacticalRadar(price, view) {
  const {plan,order,reserves,direction,focus}=view;
  const header=$('tactical-focus');
  header.textContent=focus;
  header.dataset.side=direction===1?'long':direction===-1?'short':'range';
  $('radar-source').textContent=liveBrainFresh()?'LIVE BRAIN · 3 С':'ЗА ЖУРНАЛОМ';
  const primary=$('radar-primary');
  primary.replaceChildren();
  if (plan) {
    const kind=plan.setup??plan.kind;
    const heading=element('div',null,'bar');
    heading.append(element('h3',(plan.timeframe??plan.zone?.timeframe??'')+' '+zoneName(kind)+' · '+sideLabel(plan.side)),
      element('span',order?.status==='OPEN'?'В УГОДІ':order?.status==='CANCELLING'?'ЗВІРКА':
        order?'ЛІМІТНИЙ ОРДЕР':plan.market_now?'MARKET-ГОТОВНІСТЬ':'ПЛАН LIMIT','badge'));
    const metrics=element('dl',null,'tactical-metrics');
    const risk=order?order.risk:plan.side*(plan.entry-plan.stop);
    for (const [label,value] of [['Вхід',format(plan.entry)],['Стоп',format(plan.stop)],
      ['Тейк 1',format(plan.tp1)+' · '+signed(plan.side*(plan.tp1-plan.entry)/risk)+'R'],
      ['Тейк 2',format(plan.tp2)]]) {
      const metric=element('div');
      metric.append(element('dt',label),element('dd',value));
      metrics.append(metric);
    }
    const detail=element('p',order?.status==='OPEN'?
      'PnL '+signed(grossPnlR(plan,price))+'R · '+signed(grossPnlR(plan,price)*risk,' $/од.') :
      'До входу '+format(Math.abs(price-plan.entry))+' $','tactical-distance');
    primary.append(heading,metrics,detail,
      element('small','Початковий ризик '+format(risk)+' $ · '+format(100*risk/plan.entry)+'%'+
        (number(plan.score)?' · '+plan.score+'/100':'')+(plan.local?' · PAPER':''),'muted'));
    if (order?.status!=='OPEN') {
      const progress=element('progress');
      progress.max=1;
      progress.value=Math.max(0,1-Math.abs(price-plan.entry)/Math.max(risk,state.tick??.01));
      progress.setAttribute('aria-label','Наближення ціни до входу');
      primary.append(progress);
    }
    primary.append(copyButtons(plan));
  } else {
    primary.append(element('p','Чекаємо підтвердженого сетапу.','empty-state'));
  }
  $('tactical-reserves').hidden=!reserves.length;
  $('zones').replaceChildren(...reserves.map(plan=>{
    const row=element('tr');
    const side=element('strong',sideLabel(plan.side),plan.side===1?'long':'short');
    const name=td('');
    name.append(side,element('small',zoneName(plan.kind??plan.setup)));
    const risk=plan.side*(plan.entry-plan.stop);
    row.append(name,td(format(plan.entry)),td(format(plan.stop)),td(format(plan.tp1)),
      td((plan.side*(plan.tp1-plan.entry)/risk).toFixed(2)+'R'));
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
function renderBanner(price, usable, view=tacticalRadar(price)) {
  const {order,plan,direction}=view;
  let mode='WAITING', pill='РАДАР', title='РАДАР · СКАНУЄМО РИНОК', detail='Чекаємо підтвердженого сетапу.';
  if (order?.status==='OPEN') {
    mode=order.tp1_partial?'MANAGING':'ENTERED';
    pill=order.local?'PAPER · LIVE BRAIN':'В УГОДІ';
    const pnl=grossPnlR(order,price);
    title='В УГОДІ '+sideLabel(order.side)+' @ '+format(order.entry);
    detail='PnL '+signed(pnl)+'R ('+signed(pnl*order.risk,' $/од.')+') · SL '+format(order.stop)+' | TP1 '+format(order.tp1);
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
    title='РАДАР ['+(direction?sideLabel(direction):'ДІАПАЗОН')+'] · '+sideLabel(plan.side)+' @ '+format(plan.entry);
    detail='Δ '+format(Math.abs(price-plan.entry))+' $ · SL '+format(plan.stop)+' | TP1 '+format(plan.tp1);
  } else if (!usable && !liveBrainFresh()) {
    title='ЧЕКАЄМО ОНОВЛЕННЯ ДАНИХ';
    detail='Зліпок журналу о '+stamp(state.snapshot_ts);
  } else if (liveBrainEnabled() && ['SL','TP1','TP2'].includes(brain.event?.kind) &&
      Date.now()-brain.event.ts<15*brainMinute) {
    mode='CLOSED';
    title='УГОДУ ЗАКРИТО · PAPER';
    detail=brain.event.detail;
  }
  setBannerMode(mode);
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
  $('stamp').textContent=stamp(liveBrainFresh(now)?brain.computed:state.snapshot_ts); $('instrument').textContent=state.instrument;
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
  $('state-status').textContent=usable ? 'Журнал актуальний' : state.status==='DATA_HOLD' ? 'Дані на перевірці' : 'Потрібен новий зліпок';
  if (liveBrainFresh(now)) {
    $('state-status').textContent='Live Brain · 3s Sync · журнал '+stamp(state.ledger_ts??state.snapshot_ts);
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
  renderBanner(price,usable,view);
  renderTacticalRadar(price,view);
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

function drawLevels(view=tacticalRadar()) {
  if (!series || !state) return;
  for (const line of priceLines) series.removePriceLine(line);
  priceLines=[];
  const style=window.LightweightCharts.LineStyle;
  const specs=[];
  const add=(price,title,color,dashed=true) => {
    if (number(price)) specs.push({price,title,color,lineWidth:1,lineStyle:dashed?style.Dashed:style.Solid});
  };
  const price=displayedPrice();
  const booked=view.order;
  for (const order of booked && (booked.status==='OPEN' ||
      booked.status==='PENDING'&&pendingIsCurrent(booked,price)) ? [booked] : []) {
    const prefix=(order.number ? '#'+order.number+' ' : '')+sideLabel(order.side)+' ';
    add(order.entry,prefix+(order.status==='OPEN'?(order.order_type ?? order.type ?? 'В УГОДІ'):order.status==='CANCELLING'?'СКАСУВАННЯ':'LIMIT')+
      ' @ '+format(order.entry)+(order.status==='PENDING'?' · Δ '+signed(order.entry-price,' $'):''),'#55d6ad');
    const protectedRunner = order.scale_policy === 'SCALE_OUT_1R' && order.tp1_partial &&
      order.side*(order.stop-order.entry) > 0;
    add(order.stop,prefix+(protectedRunner ? 'ЗАХИЩЕНИЙ SL' : 'SL')+' @ '+format(order.stop)+
      ' ('+signed(order.side*(order.stop-order.entry)/order.risk)+'R)',protectedRunner ? '#55d6ad' : '#ff8994',false);
    add(order.tp1,prefix+'TP1 · '+Math.round(order.tp1_fraction*100)+'% ('+
      signed(order.side*(order.tp1-order.entry)/order.risk)+'R)'+(order.tp1_partial ? ' · ВИКОНАНО' : ''),'#7db7ff');
    if (order.tp2!==order.tp1) add(order.tp2,prefix+'TP2 · залишок ('+signed(order.side*(order.tp2-order.entry)/order.risk)+'R)','#7db7ff');
  }
  const candidate = view.order ? null : view.plan;
  if (candidate) {
    add(candidate.entry, (candidate.market_now ? 'СИГНАЛ MARKET ' : 'ОЧІКУЄМО ВХІД ') + sideLabel(candidate.side) + ' @ ' + format(candidate.entry) +
      ' (Δ ' + signed(candidate.entry - price,' $') + ')', '#589aff');
    add(candidate.stop, 'СТОП-ЛОС @ ' + format(candidate.stop) + ' (' + signed(-candidate.side * (candidate.entry - candidate.stop),' $') + ')', '#ff8994', false);
    add(candidate.tp2, 'ТЕЙК-ПРОФІТ 2 @ ' + format(candidate.tp2), '#7db7ff');
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
      quote = undefined; lastSampleTime = 0; history = []; currentBar = undefined;
      if (series) series.setData([]);
      position.clear(); selectedZone=null; reactions.clear(); m5Proofs.clear(); invalidZones.clear(); exhaustedPlans.clear(); selectedPlanIds.clear(); m5Market=null; previousLiveSample=null;
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
    const ids = new Set(next.orders.map(order => order.id));
    for (const id of position.keys()) if (!ids.has(id)) position.delete(id);
    setHistory(next.candles?.[frame] ?? [], first);
    const keep=new Set(reactionRows().map(reactionKey));
    for (const [key,t] of reactions) if (!keep.has(key)&&!['INVALID','MISSED'].includes(t.stage)) reactions.delete(key);
    render();
    if (first) {
      void pollTicker(); void pollM5(); void pollBrainCandles();
      await loadCandles(true);
    }
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
setInterval(pollTicker, 3000);
setInterval(() => {
  if (state?.brain_config?.worker_interval_ms) void pollState();
},3000);
setInterval(pollState, 30_000);
setInterval(pollM5,3000);
setInterval(pollBrainCandles,3000);
document.addEventListener('visibilitychange', () => {
  pauseReactions();
  if (!document.hidden) { void pollState(); void pollTicker(); void pollM5(); void pollBrainCandles(); }
});
