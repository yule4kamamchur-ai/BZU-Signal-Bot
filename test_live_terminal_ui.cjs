/* Offline UI state-contract checks using a minimal DOM adapter. No network or browser.
 * Run: node test_live_terminal_ui.cjs
 * This verifies state transitions and chart API calls, not browser layout/rendering.
 */
'use strict';
const fs=require('node:fs'),vm=require('node:vm'),assert=require('node:assert/strict');
class Node {
  constructor(tag='div'){this.tagName=tag.toUpperCase();this.children=[];this.dataset={};this.attrs={};this.listeners={};this.style={};this.className='';this.value='';this._text='';
    this.classList={add:c=>this.className=[...new Set([...this.className.split(' '),c])].join(' ').trim(),
      remove:c=>this.className=this.className.split(' ').filter(x=>x!==c).join(' '),contains:c=>this.className.split(' ').includes(c),
      toggle:(c,v)=>{const on=v??!this.classList.contains(c);on?this.classList.add(c):this.classList.remove(c);return on;}};
  }
  set textContent(text){this._text=String(text);this.children=[];} get textContent(){return this._text+this.children.map(n=>n.textContent).join(' ');}
  append(...nodes){for(const n of nodes){n.parent=this;this.children.push(n);}} replaceChildren(...nodes){this._text='';this.children=[];this.append(...nodes);}
  setAttribute(k,v){this.attrs[k]=String(v);}getAttribute(k){return this.attrs[k]??null;}removeAttribute(k){delete this.attrs[k];}
  addEventListener(event,fn){this.listeners[event]=fn;}focus(){document.activeElement=this;}select(){}scrollIntoView(){}
  remove(){if(this.parent)this.parent.children=this.parent.children.filter(x=>x!==this);}
  closest(selector){return this.tagName==='BUTTON' && (selector.includes('data-copy')?this.dataset.copy: this.dataset.zone) ? this : this.parent?.closest(selector);}
}
const html=fs.readFileSync('index.html','utf8'), nodes=new Map();
for(const [,tag,id] of html.matchAll(/<(\w+)[^>]*\bid="([^"]+)"[^>]*>/g))nodes.set(id,new Node(tag));
const document={hidden:false,activeElement:null,body:new Node('body'),getElementById:id=>nodes.get(id),createElement:tag=>new Node(tag),
  addEventListener(){},execCommand:()=>true,querySelectorAll(selector){
    const key=selector.match(/^\[data-(.*)\]$/)?.[1];if(!key)return [];
    const dataKey=key.replace(/-([a-z])/g,(_,x)=>x.toUpperCase());
    const all=[],visit=n=>{if(n.dataset[dataKey]!==undefined)all.push(n);n.children.forEach(visit);};nodes.forEach(visit);return all;
  }};
const priceLines=[], chartCalls={data:[],text:[]};let clipboard='';
const clock={value:Date.now()};
class TestDate extends Date {static now(){return clock.value;}}
const ctx={document,window:{innerWidth:1440},navigator:{clipboard:{writeText:async text=>{clipboard=text;}}},
  Intl,URLSearchParams,AbortSignal,Date:TestDate,console,setInterval(){},clearInterval(){},WebSocket:function(){},
  fetch:async()=>({ok:false,status:503,json:async()=>({})})};
const series={setData(data){chartCalls.data=data;},update(){},applyOptions(){},priceToCoordinate:p=>400-(p-98)*100,
  createPriceLine(options){priceLines.push(options);return options;},removePriceLine(line){priceLines.splice(priceLines.indexOf(line),1);},
  attachPrimitive(primitive){primitive.attached({requestUpdate(){}});chartCalls.primitive=primitive;}};
ctx.window.LightweightCharts={CandlestickSeries:{},LineStyle:{Dashed:2,Solid:0},createChart(){return {addSeries:()=>series,
  timeScale:()=>({fitContent(){},timeToCoordinate:()=>0}),priceScale:()=>({applyOptions(){}})};}};
vm.createContext(ctx);vm.runInContext(fs.readFileSync('live_terminal.js','utf8'),ctx);
const run=source=>vm.runInContext(source,ctx),text=id=>nodes.get(id).textContent;
const now=Date.now();
const zone=(id,side,low,high,entry)=>({id,kind:side===1?'MOMENTUM_STAIRCASE_15M':'FVG_BEAR',timeframe:'15m',side,low,high,entry,
 known_ts:now-3600000,score:74,grade:'A',score_source:'CONTEXT',entry_source:'PLANNER',status:'CANDIDATE',stop:entry-side*.4,
 tp1:entry+side*.6,tp2:entry+side*1.1,reason:'ACCEPTED',actionable:true,execution_admitted:true,formation_volume:{confirmed:true},tactic:'LIMIT',reaction_eligible:true,valid_until:now+25*60000});
const long=zone('long',1,99.60,99.71,99.66),short=zone('short',-1,100.50,100.80,100.55),far={...zone('far',-1,103,103.2,103.1),actionable:false};
const order={id:'limit-1',number:7,status:'PENDING',side:1,type:'LIMIT',setup:'MOMENTUM_STAIRCASE_15M',entry:99.66,stop:99.25,
 tp1:100.24,tp2:100.75,risk:.41,score:74,grade:'A',be_trigger:100.24,be_stop:99.68,tp1_fraction:.5,remaining:1,
 effective_ts:now-60000,expires_ts:now+42*60000,filled_ts:null};
const fixture={schema:1,cockpit_schema:4,primary_bias:0,min_structural_spread:.65,radar:{LONG:long,SHORT:short},instrument:'BZ-USDT-SWAP',tick:.01,snapshot_ts:now,status:'READY',price:99.84,orders:[],
 candidates:[long,short],zones:[long,short,far],recent_events:[{kind:'CANCELLED',side:1,entry:99.66,ts:now-60000,
 reason_text:'ціна відійшла без тесту'}],candles:{},daily:{entries:2,net_r:1.1},market_regime:'RANGE'};
ctx.fixture=fixture;ctx.orderFixture=order;
const cleanFixture=JSON.parse(JSON.stringify(fixture));
(async()=>{
 // Flush the initial failed fetch before assigning the synthetic state.
 await new Promise(resolve=>setImmediate(resolve));
 run('state=fixture;stateError=false;quote={price:99.84,ts:Date.now()};render();');
 assert.match(text('state-title'),/ОЧІКУЄМО УГОДУ/);assert.equal(nodes.get('radar').children.length,2);
 assert.equal(run('visibleZones().length'),1);assert.match(text('last-event'),/Ліміт скасовано/);
 assert.equal(priceLines.length,3);
 assert.equal(document.querySelectorAll('[data-focus]').some(x=>x.dataset.zone==='far'),false);
 assert.ok(nodes.get('state-banner').classList.contains('banner-waiting'));
 run('selectedZone=null;state.orders=[orderFixture];render();');
 assert.match(text('state-title'),/ОЧІКУЄМО ВХІД.*99.66/);assert.match(text('orders'),/41 хв|42 хв/);
 assert.ok(priceLines.some(l=>l.title.includes('LIMIT')));
 assert.match(text('chart-levels'),/LIMIT/);
 const copy=document.querySelectorAll('[data-focus]').find(x=>x.dataset.copyLabel==='Entry');
 await nodes.get('orders').listeners.click({target:copy});assert.equal(clipboard,'99.66');
 run('quote={price:99.71,ts:Date.now()+1};render();');assert.ok(nodes.get('state-banner').classList.contains('near'));
 run('quote={price:99.65,ts:Date.now()+2};render();');assert.match(text('state-detail'),/торкнулася входу/);
 assert.equal(nodes.get('state-banner').dataset.mode,'WAITING');
 run('state.orders=[{...orderFixture,status:"OPEN",filled_ts:Date.now()-1000}];quote={price:99.66,ts:Date.now()+3};render();');
 assert.equal(nodes.get('state-banner').dataset.mode,'ENTERED');
 assert.ok(nodes.get('state-banner').classList.contains('banner-entered'));
 run('quote={price:99.90,ts:Date.now()+4};render();');
 assert.equal(nodes.get('state-banner').dataset.mode,'ENTERED');assert.match(text('orders'),/\+0.24/);
 run('quote={price:99.40,ts:Date.now()+5};render();');assert.equal(nodes.get('state-banner').dataset.mode,'ENTERED');
 run('quote={price:100.25,ts:Date.now()+6};render();');
 assert.match(text('orders'),/чекаємо підтвердження фіксації/);
 assert.equal(nodes.get('state-banner').dataset.mode,'ENTERED');
 assert.equal(document.querySelectorAll('[data-focus]').some(x=>x.dataset.copyLabel==='Б/У'),false);
 run('state.orders[0].tp1_hit=true;state.orders[0].tp1_partial=true;state.orders[0].remaining=.5;render();');
 assert.match(text('state-detail'),/чекаємо структуру M5/);
 assert.equal(nodes.get('state-banner').dataset.mode,'MANAGING');
 run('state.orders[0].pending_stop=99.68;state.orders[0].be_confirmed_ts=Date.now();state.orders[0].pending_stop_effective_ts=Date.now()+60000;render();');
 assert.match(text('orders'),/стоп Б\/У 99.68/);assert.doesNotMatch(text('orders'),/вже захищає/);
 run('state.orders[0].stop=99.68;delete state.orders[0].pending_stop;render();');assert.match(text('orders'),/Стоп уже захищає залишок/);
 run('state.status="DATA_HOLD";render();');assert.equal(nodes.get('state-banner').dataset.mode,'MANAGING');
 run('state.status="READY";state.orders=[{...orderFixture,status:"OPEN",side:-1,entry:100.15,stop:100.55,risk:.4,tp1:99.55,tp2:99.05}];quote={price:99.9,ts:Date.now()+6};render();');
 assert.equal(nodes.get('state-banner').dataset.mode,'ENTERED');assert.match(text('state-title'),/SHORT/);assert.match(text('orders'),/\+0.25/);
 assert.ok(priceLines.some(l=>l.title.includes('SL')&&l.title.includes('−1.00R')));
 run('state.orders[0].order_type="MARKET";render();');
 assert.match(text('state-title'),/MARKET SHORT/);
 assert.match(text('orders'),/MARKET/);
 assert.ok(priceLines.some(l=>l.title.includes('MARKET @ 100.15')));
 run('Object.assign(state.orders[0], {filled_ts:Date.now()-3*3600000,max_favorable_r:.60,max_adverse_r:.20,position_guard:{mode:"ROUND_TRIP"}});render();');
 assert.match(text('orders'),/Time in Trade: 3h 0m/);
 assert.match(text('orders'),/Peak MFE: \+0.60R/);
 assert.match(text('orders'),/ROUND-TRIP GUARD ACTIVE/);
 run('state.orders[0].position_guard.mode="STALE_WARNING";render();');
 assert.match(text('orders'),/STALE TRADE WARNING/);


 run('state.snapshot_ts=Date.now()-28*60000;render();');assert.equal(nodes.get('state-banner').dataset.mode,'ENTERED');
 run('state.snapshot_ts=Date.now();quote={price:100.1,ts:Date.now()-20000};render();');assert.match(text('state-title'),/В УГОДІ/);
 run('state.orders=[];state.zones=[];state.candidates=[];state.recent_events=[];render();');assert.match(text('state-title'),/ОЧІКУЄМО УГОДУ/);
 assert.match(text('orders'),/Скануємо ринок/);
 // UI fields use text nodes even when a public label contains markup.
 ctx.injection='<img src=x onerror=alert(1)>';run('state.zones=[{...fixture.zones[0],kind:injection}];render();');
 assert.doesNotMatch(text('zones'),/<img src=x/);assert.equal(nodes.get('zones').children[0].children[0].children[0].tagName,'STRONG');

 // A confirmed close, independent of subsequent diagnostic events, controls the red banner.
 run('state.last_closed={kind:"CLOSED",side:1,net_r:-1.15,ts:Date.now()-1000};render();');
 assert.ok(nodes.get('state-banner').classList.contains('banner-closed'));
 assert.match(text('state-detail'),/1.15R/);
 run('state.last_closed.ts=Date.now()-16*60000;render();');
 assert.equal(nodes.get('state-banner').dataset.mode,'WAITING');

 const reset=(side=1,overrides={},keepClock=false)=>{
   if(!keepClock)clock.value=Math.floor(clock.value/300000)*300000+60000;
   const entry=side===1?99.7:100.3;
   const row={id:'reaction-'+side,kind:side===1?'FVG_BULL':'FVG_BEAR',side,entry,low:entry-.05,high:entry+.05,
     stop:entry-side*.4,tp1:entry+side*.8,tp2:entry+side*1.2,score:75,score_source:'CONTEXT',status:'CANDIDATE',
     reaction_eligible:true,actionable:true,execution_admitted:true,formation_volume:{confirmed:true},timeframe:'15m',tactic:'REACTION_M5',grade:'A',known_ts:clock.value-300000,valid_until:clock.value+1500000,...overrides};
   ctx.scenario={...cleanFixture,price:100,snapshot_ts:clock.value,orders:[],candidates:[row],zones:[row],
     primary_bias:side,primary_candidate_id:row.id,radar:{LONG:side===1?row:null,SHORT:side===-1?row:null},
     reaction_config:{retracement:.06,min_samples:3,max_sample_gap_ms:15000,ready_ttl_ms:15000,max_chase_r:.25}};
   run('state=scenario;stateError=false;quote=undefined;reactions.clear();m5Proofs.clear();invalidZones.clear();exhaustedPlans.clear();selectedPlanIds.clear();previousLiveSample=null;m5Market=null;position.clear();document.hidden=false;');
   return row;
 };
 const feed=(price,step=3000)=>{
   clock.value+=step;
   ctx.tickRow={instId:'BZ-USDT-SWAP',last:String(price),ts:String(clock.value)};
   const accepted=run('acceptTicker(tickRow)');run('render()');return accepted;
 };
 const stage=()=>run('reactionPresentation(ready(Date.now()),true).stage');
 for(const side of [1,-1]){
   reset(side);
   feed(100);assert.equal(stage(),'ARMED');
   feed(100-side*.27);assert.equal(stage(),'TOUCHED');
   feed(100-side*.30);assert.equal(stage(),'TOUCHED');
   feed(100-side*.23);assert.equal(stage(),'READY');
   assert.match(text('reaction-title'),/LIVE РЕАКЦІЯ/);
   assert.equal(nodes.get('state-banner').dataset.mode,'WAITING');
   assert.equal(run('state.orders.length'),0); // A local signal is never a ledger/exchange fill.
   assert.equal(run('currentReaction().tracker.events.size'),1);
   feed(100-side*.22,1000);assert.equal(run('currentReaction().tracker.events.size'),1);
 }
 reset(-1,{entry:93.77,low:93.86,high:94.14,stop:95.87,tp1:91.46,tp2:89.78,
   trend_rel:'WITH_TREND',tactic:'MARKET',order_type:'MARKET',market_now:true,
   organic_risk:{model:'ORGANIC_OIL_V32'},market_trigger:{pattern:'POST_TOUCH_REJECTION'}});
 run('state.cockpit_schema=5;state.price=93.77;render();');
 assert.match(text('state-title'),/СИГНАЛ MARKET SHORT ВІД 93.77/);
 assert.match(text('state-detail'),/відбій від зони/);
 assert.equal(nodes.get('state-banner').dataset.mode,'WAITING');
 assert.ok(priceLines.some(l=>l.title.includes('MARKET SHORT')));
 run('state.price=92.90;render();');
 assert.equal(run('radarRows().filter(Boolean).length'),0);
 assert.equal(priceLines.length,0);
 reset();feed(99.73);feed(99.70);feed(99.77);assert.notEqual(stage(),'READY'); // Started inside, no witnessed approach.
 reset();feed(100);feed(99.73);feed(99.70);feed(99.77,20000);assert.notEqual(stage(),'READY'); // Data gap revokes touch proof.
 reset();feed(100);feed(99.29);assert.equal(run('radarRows().filter(Boolean).length'),0);feed(100);assert.equal(run('radarRows().filter(Boolean).length'),0);
 assert.match(text('alert'),/пробито/);assert.equal(run('invalidZones.size'),1);
 reset();feed(100);feed(99.73);feed(99.70);run('pauseReactions()');feed(99.77);assert.notEqual(stage(),'READY');
 reset();feed(100);feed(99.73);feed(99.70);feed(99.77);assert.equal(stage(),'READY');
 clock.value+=16000;run('render()');assert.equal(stage(),'WAIT');assert.equal(priceLines.length,0);assert.doesNotMatch(text('state-title'),/LIVE РЕАКЦІЯ/);
 reset();feed(100);feed(99.73);
 const before=run('currentReaction().tracker.lastTs');
 ctx.tickRow={instId:'BZ-USDT-SWAP',last:'99.77',ts:String(clock.value-1000)};
 assert.equal(run('acceptTicker(tickRow)'),false);assert.equal(run('currentReaction().tracker.lastTs'),before);
 ctx.tickRow.ts=String(clock.value+20000);assert.equal(run('acceptTicker(tickRow)'),false);
 reset();feed(100);feed(99.73);feed(99.70);
 ctx.tickRow={instId:'BZ-USDT-SWAP',last:'99.77',ts:String(clock.value-20000)};
 run('quote=undefined;acceptTicker(tickRow);render();');assert.doesNotMatch(text('state-title'),/LIVE РЕАКЦІЯ/);
 reset();run('m5Market={time:Math.floor(Date.now()/300000)*300,open:100,high:100.1,low:99.7,close:99.77,source:"OKX_M5"};');
 feed(100);assert.equal(stage(),'ARMED'); // REST wick is metrics only, never past touch proof.
 reset(1,{tp1:100.27});feed(100);feed(99.73);feed(99.70);feed(99.77);assert.equal(stage(),'WAIT');assert.equal(priceLines.length,0);
 reset(1,{kind:'FVG_BEAR',stop:100.94});feed(100);assert.equal(run('radarRows().filter(Boolean).length'),0);
 reset();run('state.cockpit_schema=1');feed(100);feed(99.73);feed(99.7);feed(99.77);assert.notEqual(stage(),'READY');
 reset();run('state.status="DATA_HOLD"');feed(100);feed(99.73);feed(99.7);feed(99.77);assert.notEqual(stage(),'READY');
 reset();feed(100);feed(99.73);run('document.hidden=true');feed(99.70);run('document.hidden=false');feed(99.77);assert.notEqual(stage(),'READY');
 // A touch in the previous M5 bucket cannot authorize a reaction in the next one.
 clock.value=Math.floor(clock.value/300000)*300000+294000;
 reset(1,{},true);feed(100,1000);feed(99.73,1000);feed(99.70,1000);feed(99.77,4000);assert.notEqual(stage(),'READY');
 reset();feed(100);run('state.orders=[{...orderFixture,status:"OPEN"}]');feed(99.73);feed(99.7);feed(99.77);assert.equal(stage(),'PAUSED');
 // A READY signal also expires at the M5 boundary, even before the next quote.
 clock.value=Math.floor(clock.value/300000)*300000+284000;
 reset(1,{},true);feed(100);feed(99.73);feed(99.70);feed(99.77);assert.equal(stage(),'READY');
 clock.value+=5000;run('render()');assert.equal(stage(),'WAIT');assert.equal(priceLines.length,0);
 // A pending ledger order can be monitored but a local veto never changes its status.
 const pendingRow=reset();
 ctx.pending={...order,...pendingRow,id:'existing-limit',status:'PENDING',zone:{...pendingRow},expires_ts:clock.value+600000};
 run('state.orders=[pending]');feed(100);assert.equal(run('reactionRows().length'),1);
 feed(99.29);assert.equal(stage(),'INVALID');assert.equal(run('state.orders[0].status'),'PENDING');
 assert.match(text('orders'),/Локальний вхід заблоковано|торкнулася SL/);
 // Axis labels closer than 20 screen pixels are suppressed; exact levels remain in the text legend.
 reset();feed(100);const shown=priceLines.filter(x=>x.axisLabelVisible).map(x=>series.priceToCoordinate(x.price));
 for(let i=0;i<shown.length;i++)for(let j=i+1;j<shown.length;j++)assert.ok(Math.abs(shown[i]-shown[j])>=20);
 assert.match(text('chart-levels'),/СТОП-ЛОС/);
 // An admitted B+ plan can prove a fresh local reaction.
 const approvedReaction=(side=1)=>{
   const row=reset(side);Object.assign(row,{score:60,grade:'B+',timeframe:'15m',score_source:'ZONE_HEURISTIC',score_model:'ZONE_5_FACTOR_V2',
     tactic:'REACTION_M5',status:'CANDIDATE',entry_source:'ENGINE_PLAN',plan_basis:'ENGINE_GEOMETRY',rr1:2,rr2:3,display_eligible:true});
   run('state.cockpit_schema=4;state.plan_pool=state.candidates;');return row;
 };
 approvedReaction();feed(100);assert.match(text('radar'),/клас B\+/);assert.match(text('zones'),/TP2/);
 feed(99.73);feed(99.70);feed(99.77);assert.equal(stage(),'READY');assert.equal(run('state.orders.length'),0);
 // Ordinary deep penetration is not a terminal invalidation until the actual SL is breached.
 approvedReaction();feed(100);feed(99.68);assert.equal(stage(),'TOUCHED');feed(99.77);assert.equal(stage(),'READY');
 // SL invalidation immediately selects the next resistance from the existing cached pool.
 const first=approvedReaction(-1);const second={...first,id:'higher-resistance',entry:101.3,low:101.25,high:101.35,stop:101.7,tp1:99.98,tp2:99.82};
 ctx.second=second;run('state.plan_pool.push(second);state.zones.push(second);');
 feed(100);assert.equal(run('radarRows()[1].id'),first.id);
 feed(100.71);assert.equal(run('radarRows()[1].id'),'higher-resistance');
 assert.match(text('radar'),/101.30/);assert.ok(priceLines.some(l=>l.title.includes('СТОП-ЛОС')&&Math.abs(l.price-101.7)<.0001));
 assert.match(text('zones'),/SL ПРОБИТО/);
 // The same invalidated zone cannot reappear simply because the quote returns below it.
 feed(100.67);assert.equal(run('radarRows()[1].id'),'higher-resistance');
 // A weak or blocked plan remains in the table and cannot add chart lines.
 const weak=approvedReaction();weak.score=40;weak.grade='C';weak.tactic='OBSERVE';weak.reaction_eligible=false;
 feed(100);feed(99.73);feed(99.70);feed(99.77);assert.notEqual(stage(),'READY');assert.doesNotMatch(text('radar'),/40\/100/);assert.match(text('zones'),/Спостерігаємо за зоною/);assert.match(text('zones'),/40\/100/);
 assert.equal(priceLines.length,0);
 // With no pending order, repeated renders never toggle the near-limit class by passing undefined.
 approvedReaction();feed(100);for(let i=0;i<5;i++)run('render()');assert.equal(nodes.get('state-banner').classList.contains('near'),false);
 run('state.orders=[{...orderFixture,side:-1,entry:null}]');run('render()');
 assert.equal(nodes.get('state-banner').classList.contains('near'),false);assert.doesNotMatch(text('alert'),/Дотик ціни до входу/);
 // Null, blank, boolean and zero API fields do not become valid quote numbers.
 reset();for(const value of [null,'',false,'0','   ']){ctx.badQuote={instId:'BZ-USDT-SWAP',last:value,ts:String(clock.value)};assert.equal(run('acceptTicker(badQuote)'),false);}
 // The main plan's exact Entry, SL and TP1 are drawn even while Entry is near the live quote.
 approvedReaction();feed(99.82);assert.ok(priceLines.some(l=>l.title.includes('СТОП-ЛОС')&&l.axisLabelVisible));
 assert.ok(priceLines.some(l=>l.title.includes('ТЕЙК-ПРОФІТ 1')&&l.axisLabelVisible));
 assert.ok(priceLines.some(l=>Math.abs(l.price-99.7)<.0001&&l.axisLabelVisible));
 // A high-score plan with a valid shape but no admission must remain hidden.
 approvedReaction();run('state.plan_pool[0].actionable=false;render();');
 assert.equal(priceLines.length,0);assert.doesNotMatch(text('state-detail'),/Робоча зона/);
 assert.equal(run('radarRows().filter(Boolean).length'),0);
 // Ordinary volume earns no points but does not veto an admitted plan.
 approvedReaction();run('state.plan_pool[0].formation_volume={confirmed:false};render();');
 assert.ok(priceLines.length);assert.equal(run('radarRows().filter(Boolean).length'),1);
 // Once TP1 has traded, bouncing back does not resurrect the same plan.
 feed(100.5);assert.equal(priceLines.length,0);
 assert.match(text('zones'),/ціль пройдена/);
 feed(100);assert.equal(run('radarRows().filter(Boolean).length'),0);
 // Context bands have no entry/stop/target lines.
 run('state.context_bands=[{id:"context",side:1,kind:"DEMAND_OB",timeframe:"1H",low:99.4,high:99.5}];render();');
 assert.equal(run('visibleZones().length'),1);assert.equal(priceLines.length,0);
 // Blocked and distant reference rows stay out of the headline and chart.
 const blocked=reset(1,{actionable:false,score:59,grade:'C',risk:.4,rr1:2,rr2:3,
   gate_reason:'BLOCKED_BY_TREND'});
 const reserve={...blocked,id:'reserve',kind:'DEMAND_OB',entry:98.7,low:98.6,high:98.8,
   stop:98.3,tp1:100.2,tp2:100.9,gate_reason:'DISTANT_ZONE'};
 ctx.refRows=[blocked,reserve];
 run('state.cockpit_schema=5;state.plan_pool=refRows;state.zones=refRows;render();');
 assert.match(text('state-title'),/ОЧІКУЄМО УГОДУ/);
 assert.doesNotMatch(text('state-title'),/99.70|98.70/);
 assert.match(text('radar'),/Далекий резерв @ 98.70/);
 assert.equal(priceLines.length,0);assert.equal(run('reactionRows().length'),0);
 // Only a server-admitted reversal may oppose the displayed trend.
 approvedReaction(-1);
 run('state.primary_bias=1;state.plan_pool[0].kind="SWEEP_REVERSAL";render();');
 assert.equal(priceLines.length,0);
 run('state.plan_pool[0].reversal_override=true;render();');
 assert.ok(priceLines.length);

 // A newly opened tab already inside the zone still waits for a closed M5.
 const boundary=Math.floor(clock.value/300000)*300000+300000;
 clock.value=boundary+2000;
 reset(1,{entry_condition:'IN_ZONE_M5_RECLAIM',known_ts:boundary-900000},true);
 run('state.cockpit_schema=5;state.plan_pool=state.candidates;');
 feed(99.74,1000);assert.equal(stage(),'TOUCHED');
 const row=(ts,open,high,low,close,confirmed)=>[ts,open,high,low,close,100,0,0,confirmed?'1':'0'].map(String);
 const previous=row(boundary-600000,99.82,99.87,99.70,99.74,true);
 const last=row(boundary-300000,99.72,99.80,99.62,99.77,false);
 ctx.fetch=async()=>({ok:true,json:async()=>({code:'0',data:[last,previous]})});
 await run('pollM5()');assert.notEqual(stage(),'READY');
 last[8]='1';await run('pollM5()');assert.equal(stage(),'READY');
 assert.match(text('alert'),/Відбій із тінню M5/);
 assert.equal(run('state.orders.length'),0);
 const observed=run('m5Proofs.size');await run('pollM5()');assert.equal(run('m5Proofs.size'),observed);
 feed(99.95,1000);assert.notEqual(stage(),'READY');
 // The organic plan uses gross targets and the same risk window for a live recheck.
 const oil = reset(1, {entry:99.7, stop:98.2, risk:1.5, tp1:101.22, tp2:102.43,
   organic_risk:{model:'ORGANIC_OIL_V32',atr_1h:.8,adr_d1:3.1,adr_days:14,
     session_multiplier:1.2,macro_anchor:{timeframe:'1H',level:98.4}}});
 run('state.cockpit_schema=5;state.organic_risk_profile={model:"ORGANIC_OIL_V32"};state.plan_pool=state.candidates;');
 feed(100);
 assert.ok(priceLines.some(l=>l.title.includes('СТОП-ЛОС')));
 assert.match(text('radar'),/Органічний стоп 98.20/);
 assert.match(text('radar'),/1.88× ATR 1H.*ADR 3.10/);
 assert.match(text('radar'),/1.01R.*1.82R/);
 assert.ok(run('m5QuotePlan(state.plan_pool[0],99.7)'));
 assert.equal(run('m5QuotePlan(state.plan_pool[0],99.76)'),null);
 oil.organic_risk=null;run('render()');assert.equal(priceLines.length,0);
 // A scored trend pre-limit stays visible beyond the old distance and skips the M5 watch.
 reset(1, {entry:99.7, stop:98.2, risk:1.5, tp1:101.22, tp2:102.43, kind:'BREAKER_BLOCK',
   trend_rel:'WITH_TREND', score:52, min_score:52, entry_max_distance:1.994, limit_ready:true,
   organic_risk:{model:'ORGANIC_OIL_V32',atr_1h:.8,adr_d1:3.1,adr_days:14}});
 run('state.cockpit_schema=5;state.plan_pool=state.candidates;state.candidate_rules={max_distance:.65};');
 feed(100.9);
 assert.match(text('radar'),/Рівень допущено до лімітки/);
 run('state.orders=[{...state.plan_pool[0],number:201,status:"PENDING",type:"LIMIT",expires_ts:Date.now()+3600000}];state.plan_pool=[];render();');
 assert.match(text('orders'),/201.*99.70/);
 assert.equal(run('reactionRows().length'),0);
 assert.equal(run('chartOrders().length'),1);
 console.log('PASS: offline UI lifecycle, actionable radar, chart cleanup and reaction scenarios.');
})().catch(error=>{console.error(error);process.exitCode=1;});
