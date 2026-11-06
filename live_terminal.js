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
const reactions=new Map();
const invalidZones=new Set(), selectedPlanIds=new Map();
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
  if (state.cockpit_schema===3&&validCandidate(row)&&livePlanState(row,price)==='INVALID') return 'SL ПРОБИТО · ЗОНУ СПИСАНО';
  if (row.status==='REFERENCE') return 'Довідково · '+({INCOMPLETE_PLAN:'неповний план',SCORE_BELOW_65:'бал нижче 65',SCORE_BELOW_58:'бал нижче 58',INSUFFICIENT_ROOM:'мало простору до цілі',STOP_OUTSIDE_RISK_BOUNDS:'ризик поза лімітом',DISTANT_ZONE:'далека зона',STOP_ALREADY_BREACHED:'SL вже пробито після формування',
    CONTEXT_NOT_EVALUATED:'лише оцінка патерну',RR_BELOW_1_35:'недостатній R:R',ENGINE_CONFIRMATION_REQUIRED:'чекаємо підтвердження',
    BLOCKED_BY_TREND:'проти тренду',ENTRY_OUTSIDE_REFINED_ZONE:'Entry поза звуженою зоною',ZONE_NOT_AHEAD_OF_PRICE:'ціна вже у зоні або за нею',
    STRUCTURAL_SPREAD_CONFLICT:'конфлікт дистанції',LOWER_PRIORITY:'другорядний рівень',EXPIRED:'строк минув',
    NOT_IN_FINAL_REACTION_PLANS:'не пройдено фінальний допуск',ATR_UNAVAILABLE:'бракує ATR',ZONE_TIME_UNKNOWN:'невідомий час зони'}[row.gate_reason]??'без допуску до входу');
  return ({ OPEN:'У супроводі', PENDING:'Ліміт активний', CANCELLING:'Скасування ліміту',
    PREPLAN:'Попередній план · очікує допуску', WAIT_CONFIRMATION:'Підтвердження ' + (row.confirmation_timeframe || 'структури'),
    BLOCKED:'Вхід заблоковано', REFERENCE:'Довідковий рівень', OBSERVING:'Довідкова зона' })[row.status] ??
    (Math.abs(row.entry - price) <= .08 ? 'Готується вхід · ціна поруч' : 'Кандидат · чекаємо підхід');
}
// Independent validation at the public-data boundary. A chart label cannot authorize a trade.
function validCandidate(row) {
  if (!row || ![1,-1].includes(row.side)) return false;
  const locked={DEMAND_OB:1,FVG_BULL:1,BULLISH_BREAKER:1,BULL_BREAKER:1,BREAKER_BULL:1,SWEEP_LOW:1,IFVG_BULL:1,
    SUPPLY_OB:-1,FVG_BEAR:-1,BEARISH_BREAKER:-1,BEAR_BREAKER:-1,BREAKER_BEAR:-1,SWEEP_HIGH:-1,IFVG_BEAR:-1};
  const neutral=['CONFIRMED_FVG','BREAKER_FLIP_15M','MOMENTUM_STAIRCASE_15M','DISPLACEMENT_CATCHER_15M',
    'LIQUIDITY_RAID_ENGULFING_15M','STRUCTURAL_LEVEL','SESSION_SWEEP_ENGULFING',
    'SESSION_SWEEP_ENGULFING_15M','SWEEP_RECLAIM_ZONE','SESSION_OB'];
  if (locked[row.kind] ? locked[row.kind]!==row.side : !neutral.includes(row.kind)) return false;
  if (!['low','high','entry','stop','tp1','tp2'].every(k=>number(row[k])&&row[k]>0)||!number(row.score)||row.score<0) return false;
  const risk=row.side*(row.entry-row.stop);
  const proximal=row.side===1?row.high:row.low;
  const outside=row.entry<row.low || row.entry>row.high;
  const preview=row.score_source==='ZONE_HEURISTIC'&&row.score_model==='ZONE_5_FACTOR_V1';
  return row.low<row.high && risk>0 && row.side*(row.tp1-row.entry)/risk>=(preview?0:1.35)-1e-8 &&
    row.side*(row.tp2-row.tp1)>=-1e-8 && (preview||row.score>=65) && row.score<=100 && number(row.known_ts) && row.known_ts<=state.snapshot_ts && (preview||row.score_source==='CONTEXT') &&
    (!outside || (row.side*(row.entry-proximal)>=0 && row.side*(row.entry-proximal)<=.03000001+(preview?(state.tick||.01):0)));
}
function poolRows() { return state.plan_pool??state.candidates??[]; }
function zoneFingerprint(row) {
  return [state.instrument,row.kind,row.timeframe,row.side,row.source_low??row.low,row.source_high??row.high,row.known_ts].join('|');
}
function livePlanState(row,price) {
  if (invalidZones.has(zoneFingerprint(row)) || row.side*(price-row.stop)<=0) return 'INVALID';
  if (number(row.valid_until)&&row.valid_until<=Date.now()) return 'EXPIRED';
  const tracker=reactions.get(reactionKey(row));
  if (tracker?.stage==='READY') return 'READY';
  if (tracker?.stage==='TOUCHED') return 'TOUCHED';
  if (row.low<=price&&price<=row.high) return 'IN_ZONE';
  return row.side*(price-row.entry)>0?'APPROACH':'WAIT_REACTION';
}
function radarRows(price = displayedPrice()) {
  const bias=state.primary_bias||0;
  const rows=[1,-1].map(side=>{
    let choices=poolRows().filter(row=>row.side===side&&validCandidate(row)&&row.display_eligible!==false&&
      (!bias||side===bias)&&!['OPEN','PENDING','CANCELLING'].includes(row.status)&&row.gate_reason!=='STOP_ALREADY_BREACHED'&&
      !['INVALID','EXPIRED'].includes(livePlanState(row,price)));
    // A witnessed touch owns the live check until resolved; do not chase another zone mid-rejection.
    const held=choices.find(row=>selectedPlanIds.get(side)===row.id&&['TOUCHED','READY'].includes(livePlanState(row,price)));
    choices.sort((a,b)=>Boolean(b.reaction_eligible)-Boolean(a.reaction_eligible)||(!a.reaction_eligible&&!b.reaction_eligible?Math.abs(a.entry-price)-Math.abs(b.entry-price):b.score-a.score)||
      Math.abs(a.entry-price)-Math.abs(b.entry-price));
    const selected=held??choices[0]??null;
    if (selected) selectedPlanIds.set(side,selected.id); else selectedPlanIds.delete(side);
    return selected;
  });
  if (rows[0]&&rows[1] && (rows[1].entry-rows[0].entry < Math.max(.65,state.min_structural_spread||0) || rows[0].high>=rows[1].low)) {
    const keep=bias || (rows[0].score>=rows[1].score?1:-1);rows[keep===1?1:0]=null;
  }
  return rows;
}
function mainPlan(rows=radarRows().filter(Boolean)) {
  return rows.find(r=>r.id===state.primary_candidate_id)||rows.find(r=>r.side===state.primary_bias)||[...rows].sort((a,b)=>b.score-a.score)[0]||null;
}
function reactionRows() {
  if (![2,3].includes(state.cockpit_schema) || state.orders.some(o=>o.status==='OPEN') || !ready(Date.now())) return [];
  let rows=radarRows().filter(r=>r?.reaction_eligible&&r.score>=(state.cockpit_schema===3?58:65));
  for (const order of state.orders) {
    if (order.status!=='PENDING' || order.expires_ts<=Date.now()) continue;
    const row={...order,...order.zone,id:'order:'+order.id,score_source:'CONTEXT',reaction_eligible:true,valid_until:order.expires_ts};
    if (validCandidate(row) && (!state.primary_bias || row.side===state.primary_bias)) {
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
        row.side*(price-t.readyPrice)<-tick) {
      t.stage='MISSED';t.message='Реакція втратила актуальність. Не наздоганяємо ціну.';
    }
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
      if (row.side*(price-row.entry)>(cfg.max_chase_r??.25)*risk || newRR<(state.cockpit_schema===3?1.4:1.35)-1e-8) {
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
  for (const row of poolRows()) if (validCandidate(row)&&!['OPEN','PENDING','CANCELLING'].includes(row.status)&&row.side*(market.price-row.stop)<=0&&!invalidZones.has(zoneFingerprint(row))) {
    invalidZones.add(zoneFingerprint(row));const t=reactionTracker(row);t.stage='INVALID';
    notify(row,t,'ZONE_SL','SL плану '+format(row.stop)+' пробито · зону списано локально');
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
  if (!current) return {stage:'WAIT',title:'Чекаємо реакційний план від 58/100',detail:'Слабкі, неповні та протитрендові рівні залишаються довідковими.'};
  const {row,tracker:t}=current;
  const expired=t.stage==='READY' && (Date.now()-t.readyTs>(state.reaction_config?.ready_ttl_ms??15000)||Math.floor(Date.now()/300000)!==t.bucket);
  const stage=expired?'MISSED':t.stage;
  return {stage,row,tracker:t,title:({UNARMED:'Чекаємо свіжий підхід',ARMED:'План підготовлено · чекаємо дотик',
    TOUCHED:'У ЗОНІ · чекаємо реакцію M5',READY:'🎯 СНАЙПЕРСЬКИЙ СИГНАЛ · LIVE РЕАКЦІЯ M5',INVALID:'ЛОКАЛЬНИЙ ВХІД ЗАБЛОКОВАНО',
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
async function pollM5() {
  if (!state||m5Busy||document.hidden) return;
  m5Busy=true;const instrument=state.instrument;
  try {
    const body=await json(OKX+'/candles?'+new URLSearchParams({instId:instrument,bar:'5m',limit:'2'}));
    if (body.code!=='0'||state.instrument!==instrument) return;
    const bucket=Math.floor(Date.now()/300000)*300;
    const raw=body.data?.find(r=>Number(r[0])/1000===bucket);
    if (!raw) return;
    const [ms,open,high,low,close]=raw.slice(0,5).map(Number);
    if (![ms,open,high,low,close].every(number)||Math.min(open,high,low,close)<=0||low>Math.min(open,close)||high<Math.max(open,close)) return;
    // This OHLC supplies metrics only. It cannot retroactively prove an observed touch/rejection.
    m5Market={time:ms/1000,open,high,low,close,source:'OKX_M5',received:Date.now()};
  } catch { /* Tick observations remain explicitly partial. */ }
  finally {m5Busy=false;}
}

function visibleZones() {
  const rows=state.zones.filter(z=>!['ZONE_DIRECTION_MISMATCH','INVALID_STOP_DIRECTION'].includes(z.gate_reason));
  if (showAllZones) return rows;
  const selected=radarRows().filter(Boolean).filter(r=>reactions.get(reactionKey(r))?.stage!=='INVALID');
  const ids=new Set(selected.map(z=>z.id));
  return rows.filter(z=>ids.has(z.id)||z.id===selectedZone||state.orders.some(o=>z.order_id===o.id));
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
    name.title = z.gate_reason ?? z.reason_text ?? z.reason ?? 'Зона без оціненого сетапу';
    const score = td(scoreText(z));
    if (z.score_source==='PATTERN' && number(z.score)) score.append(element('small','Патерн'));
    score.title = z.score_breakdown ? z.score_breakdown.map(c=>c.criterion+': '+c.earned+'/'+c.maximum).join(' · ') : z.score_source === 'PATTERN' ? 'Бал патерну; повна перевірка входу ще не пройдена' :
      z.score_source === 'UNASSESSED' ? 'Рушій ще не оцінив сетап для цієї зони' : 'Оцінка контексту рушієм';
    const entry = td('');
    entry.append(element('strong', format(z.entry)), element('small', distanceText(z.entry, price)),
      element('small', entrySource(z)),element('small','SL '+format(z.stop)+' · TP1 '+format(z.tp1)),
      element('small','TP2 '+format(z.tp2)+' · R:R '+(number(z.rr1)?z.rr1.toFixed(2):'не визначено')));
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
function tacticText(row) {
  return row.tactic==='LIMIT'?'КЛАС A · пряма лімітка'+(row.execution_admitted?' · допуск пройдено':' · попередній план, очікує допуску'):row.tactic==='REACTION_M5'?'КЛАС '+row.grade+' · вхід лише за реакцією M5':'Довідковий план · вхід заблоковано';
}
function entrySource(row) {
  return ({ STRUCTURAL_PREPLAN:'Попередній структурний план', ENGINE_PLAN:'План рушія', ORDER:'Ордер за журналом', PLANNER:'Розрахунок рушія', ANCHOR:'Попередній рівень сетапу',
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
  if (order.status==='PENDING') {
    const watching=reactionRows().find(r=>r.id==='order:'+order.id);
    if (watching && reactions.get(reactionKey(watching))?.stage==='INVALID') return 'Локальний вхід заблоковано через пробій. Лімітка в журналі ще чинна; перевірте її у брокера.';
  }
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
  const candidates=radarRows().filter(Boolean);
  const candidate=mainPlan(candidates);
  let mode='SCANNING', title='Скануємо ринок · шукаємо сетап', detail='Чекаємо підтверджених умов входу.', pill='СКАНУВАННЯ';
  if (candidate) {
    mode=candidate.side===-1?'PRIMARY_SHORT':'PRIMARY_LONG'; pill='ГОЛОВНИЙ ПРІОРИТЕТ';
    title=(candidate.tactic==='OBSERVE'?'ДОВІДКОВИЙ ПЛАН: ':candidate.entry_source==='STRUCTURAL_PREPLAN'?'ПОПЕРЕДНІЙ ПЛАН: ':'ЧЕКАЄМО ЗОНУ: ') + sideLabel(candidate.side) + ' ' +
      (number(candidate.low) ? '[' + format(candidate.low) + '–' + format(candidate.high) + ']' : format(candidate.entry));
    detail=(candidate.setup ?? candidate.kind) + ' · '+scoreText(candidate)+' · '+tacticText(candidate)+' · ' + statusText(candidate,price) + ' · Δ ' + distanceText(candidate.entry,price) +
      ' · ' + entrySource(candidate) + ' ' + format(candidate.entry) +
      (number(candidate.stop) ? ' | SL ' + format(candidate.stop) : '') +
      (number(candidate.tp1) ? ' | TP1 ' + format(candidate.tp1) : '')+(number(candidate.tp2)?' | TP2 '+format(candidate.tp2):'')+(number(candidate.rr1)?' | R:R '+candidate.rr1.toFixed(2):'');
  }
  if (!candidate && state.primary_bias && !active && !pending) {
    mode=state.primary_bias===-1?'PRIMARY_SHORT':'PRIMARY_LONG';
    title='ПРІОРИТЕТ '+sideLabel(state.primary_bias)+' · чекаємо якісний план';
    detail='Немає повного незламаного плану. Довідкові рівні — у таблиці; чекаємо новий зліпок.';
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
  const reaction=reactionPresentation(usable,live);
  if (usable && live && !active && ['READY','INVALID','TOUCHED','MISSED'].includes(reaction.stage)) {
    mode=reaction.stage==='READY'?'REACTION_READY':reaction.stage==='INVALID'?'REACTION_BLOCKED':'REACTION_WATCH';
    title=reaction.title+(reaction.row?' · '+sideLabel(reaction.row.side):'');
    detail=reaction.detail;
    if (reaction.stage==='READY') detail+=' @ '+format(reaction.tracker.readyPrice)+' | SL '+format(reaction.row.stop)+' | TP1 '+format(reaction.row.tp1)+' | R:R '+reaction.tracker.rr.toFixed(2);
    if (pending) detail+=' Лімітка за журналом не змінена; перевірте її у брокера.';
    pill='ЛОКАЛЬНИЙ LIVE ФІЛЬТР';
  }
  if (!usable) {
    mode=state.status==='DATA_HOLD' ? 'DATA_HOLD' : 'STALE'; pill='СИГНАЛИ ПРИЗУПИНЕНО';
    title=state.status==='DATA_HOLD' ? 'ПЕРЕВІРКА ДАНИХ · DATA HOLD' : 'СТАН БОТА ЗАСТАРІВ / НЕДОСТУПНИЙ';
    detail='Останній зліпок: ' + stamp(state.snapshot_ts) + '. Рівні нижче — останні відомі, сповіщення призупинено.';
  } else if (!live) detail += ' · Дистанції за ціною зі зліпка.';
  const banner=$('state-banner'); banner.dataset.mode=mode;
  const isNear=Boolean(usable && live && !active && pending?.status==='PENDING' && number(pending.entry) && pending.entry>0 &&
    !orderTracker(pending).touch && !orderTracker(pending).end && Math.abs(price-pending.entry)<=.08000001);
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
  radarRows(price).forEach((row,index) => {
    const side=index===0 ? 1 : -1;
    const card=element('article',null,'candidate'); card.dataset.side=side;
    card.append(element('h3',sideLabel(side),side===1?'long':'short'));
    card.dataset.priority=state.primary_bias===side?'PRIMARY':state.primary_bias?'COUNTER':'RANGE';
    if (!row) {card.append(element('p',state.primary_bias&&state.primary_bias!==side?
      '⛔ Проти тренду. Лише довідковий резерв; чекаємо підтвердженого розвороту.':
      'Слабкі або неповні зони відфільтровано. Чекаємо план ≥65/100.','reason'));container.append(card);return;}
    card.dataset.reaction=reactions.get(reactionKey(row))?.stage??'WAIT';
    if (card.dataset.priority==='PRIMARY') card.append(element('p','ГОЛОВНИЙ ПРІОРИТЕТ · ЗА ТРЕНДОМ','priority-label'));
    card.append(element('p',(row.timeframe ?? '') + ' ' + (row.setup ?? row.kind ?? 'Сетап'),'setup'),
      element('p',format(row.entry),'candidate-entry'),element('p',entrySource(row),'muted'),
      element('p',distanceText(row.entry,price),'distance'));
    const localStage=card.dataset.reaction;
    const badge=element('span',localStage==='INVALID'?'Локальний вхід заблоковано':localStage==='MISSED'?'Реакцію пропущено':tacticText(row),'badge');
    card.append(badge,element('p',scoreText(row) + (row.score_source==='PATTERN' ? ' · патерн' : ''),'reason'));
    if (number(row.stop)) card.append(element('p','SL ' + format(row.stop) + ' · TP1 ' + format(row.tp1)+' · TP2 '+format(row.tp2)+' · R:R '+(number(row.rr1)?row.rr1.toFixed(2):'не визначено'),'reason'));
    if (row.score_breakdown) {
      const detail=element('details',null,'score-criteria');detail.append(element('summary','За що нараховано бал?'));
      const names={REGIME_ALIGNMENT:'Тренд',UNTESTED:'Свіжість',HTF_OR_SWEEP:'Старший TF / sweep',DISPLACEMENT_OR_FVG:'Імпульс / FVG',ROOM_TO_RUN:'Простір до цілі'};
      for(const c of row.score_breakdown)detail.append(element('p',(names[c.criterion]??c.criterion)+' · '+c.earned+'/'+c.maximum));
      card.append(detail);
    }
    if (row.engine_reason&&row.engine_reason!=='NO_EVALUATED_SETUP')card.append(element('p','Перевірка рушія: '+(row.reason_text??row.engine_reason),'reason'));
    if (row.plan_basis==='STRUCTURAL_PREPLAN')card.append(element('p','Розрахунковий план, не виставлений ордер.','reason'));
    else if (row.reason_text) card.append(element('p',row.reason_text,'reason'));
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
    const fields=[['Сетап',order.setup ?? 'Не вказано'],['Бал / клас за журналом',scoreText(order)],['Entry / SL',format(order.entry)+' / '+format(order.stop)],
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
    upColor: '#4bd2a5', downColor: '#f17785', borderVisible: false,
    wickUpColor: '#4bd2a5', wickDownColor: '#f17785',
    autoscaleInfoProvider: original => {
      const info=original();
      if (!info || !state) return info;
      const values=state.orders.flatMap(o=>[o.entry,o.stop,o.tp1,o.tp2,o.be_trigger]).filter(number);
      const main=mainPlan();
      if (main) values.push(main.entry,main.stop,main.tp1);
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
  const specs=[];
  const add=(price,title,color,dashed=true) => {
    if (number(price)) specs.push({price,title,color,lineWidth:1,lineStyle:dashed?style.Dashed:style.Solid});
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
  const mainPlans=radarRows(price).filter(Boolean);
  const primary=mainPlan(mainPlans);mainPlans.sort((a,b)=>(b.id===primary?.id)-(a.id===primary?.id));
  for (const zone of mainPlans) {
    if (state.orders.some(o=>o.id===zone.order_id)) continue;
    add(zone.entry,(zone.timeframe ?? '')+' '+zone.kind+' '+sideLabel(zone.side)+
      (' · '+(zone.grade??'ПЛАН')+' '+(zone.tactic??'РІВЕНЬ')),zone.side===1?'#4c9f88':'#ad6577');
    if (number(zone.stop)&&number(zone.tp1)) {
      add(zone.stop,sideLabel(zone.side)+' SL @ '+format(zone.stop),'#ff8994',false);
      add(zone.tp1,sideLabel(zone.side)+' TP1 @ '+format(zone.tp1),'#7db7ff');
      add(zone.tp2,sideLabel(zone.side)+' TP2 @ '+format(zone.tp2),'#6698d0');
    }
  }
  series.applyOptions({priceFormat:{type:'price',precision:Math.max(2,(String(state.tick).split('.')[1] ?? '').length),minMove:state.tick || .01}});
  series.applyOptions({lastValueVisible:mainPlans.length===0});
  const labels=mainPlans.length?[]:[price,(currentBar??history.at(-1))?.close].filter(number).map(p=>series.priceToCoordinate(p)).filter(y=>y!==null);
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
      position.clear(); selectedZone=null; reactions.clear(); invalidZones.clear(); selectedPlanIds.clear(); m5Market=null; previousLiveSample=null;
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
    const keep=new Set(reactionRows().map(reactionKey));
    for (const [key,t] of reactions) if (!keep.has(key)&&!['INVALID','MISSED'].includes(t.stage)) reactions.delete(key);
    render();
    void pollTicker(); void pollM5();
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
