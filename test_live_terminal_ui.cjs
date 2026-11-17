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
const clock={value:Date.parse('2026-11-17T08:01:00Z')};
class TestDate extends Date {static now(){return clock.value;}}
const storage=new Map();
const ctx={localStorage:{getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value),
  removeItem:key=>storage.delete(key)},document,window:{innerWidth:1440},navigator:{clipboard:{writeText:async text=>{clipboard=text;}}},
  Intl,URLSearchParams,AbortSignal,Date:TestDate,console,setInterval(){},clearInterval(){},WebSocket:function(){},
  fetch:async()=>({ok:false,status:503,json:async()=>({})})};
const series={setData(data){chartCalls.data=data;},update(){},applyOptions(){},priceToCoordinate:p=>400-(p-98)*100,
  createPriceLine(options){priceLines.push(options);return options;},removePriceLine(line){priceLines.splice(priceLines.indexOf(line),1);},
  attachPrimitive(primitive){primitive.attached({requestUpdate(){}});chartCalls.primitive=primitive;}};
ctx.window.LightweightCharts={CandlestickSeries:{},LineStyle:{Dashed:2,Solid:0},createChart(){return {addSeries:()=>series,
  timeScale:()=>({fitContent(){},timeToCoordinate:t=>200+(t-clock.value/1000)/30}),priceScale:()=>({applyOptions(){}})};}};
vm.createContext(ctx);vm.runInContext(fs.readFileSync('live_terminal.js','utf8'),ctx);
const run=source=>vm.runInContext(source,ctx),text=id=>nodes.get(id).textContent;
const now=clock.value;
const zone=(id,side,low,high,entry)=>({id,kind:side===1?'MOMENTUM_STAIRCASE_15M':'FVG_BEAR',timeframe:'15m',side,low,high,entry,
 known_ts:now-3600000,score:74,grade:'A',score_source:'CONTEXT',entry_source:'PLANNER',status:'CANDIDATE',stop:entry-side*.4,
 tp1:entry+side*.6,tp2:entry+side*1.1,reason:'ACCEPTED',actionable:true,execution_admitted:true,formation_volume:{confirmed:true},tactic:'LIMIT',reaction_eligible:true,valid_until:now+25*60000});
const long=zone('long',1,99.60,99.71,99.66),short=zone('short',-1,100.50,100.80,100.55),far={...zone('far',-1,103,103.2,103.1),actionable:false};
const order={id:'limit-1',number:7,status:'PENDING',side:1,type:'LIMIT',setup:'MOMENTUM_STAIRCASE_15M',entry:99.66,stop:99.25,
 tp1:100.24,tp2:100.75,risk:.41,score:74,grade:'A',be_trigger:100.24,be_stop:99.68,tp1_fraction:.5,remaining:1,
 placed_ts:now-60000,effective_ts:now-60000,
 zone:{kind:'DEMAND_OB',timeframe:'15m',low:99.52,high:99.7,origin_ts:now-45*60000},
 liquidity:{bsl:{price:100.82},ssl:{price:99.16}},expires_ts:now+42*60000,filled_ts:null};
const fixture={schema:1,cockpit_schema:4,primary_bias:0,min_structural_spread:.65,radar:{LONG:long,SHORT:short},instrument:'BZ-USDT-SWAP',tick:.01,snapshot_ts:now,status:'READY',price:99.84,orders:[],
 candidates:[long,short],zones:[long,short,far],recent_events:[{kind:'CANCELLED',side:1,entry:99.66,ts:now-60000,
 reason_text:'ціна відійшла без тесту'}],candles:{},daily:{entries:2,net_r:1.1},market_regime:'RANGE'};
ctx.fixture=fixture;ctx.orderFixture=order;
const cleanFixture=JSON.parse(JSON.stringify(fixture));
ctx.fixtureBackup=cleanFixture;

(async()=>{
 await new Promise(resolve=>setImmediate(resolve));
 run(`state={...fixtureBackup,market_regime:'BEARISH_ACTIVE',primary_bias:-1,price:89.54,candidates:[
    {id:'bad-rr',kind:'TURTLE_SOUP',side:-1,entry:89.54,stop:90.19,tp1:89.48,tp2:88,
     score:95,actionable:true,timeframe:'15m'},
    {id:'bad-score',kind:'FVG_BEAR',side:-1,entry:89.64,stop:90.29,tp1:88.60,tp2:88.3,
     score:59,actionable:true},
    {id:'blocked',kind:'TURTLE_SOUP',side:-1,entry:89.67,stop:90.32,tp1:88.70,tp2:88,
     score:80,actionable:true,gate_reason:'BLOCKED_LOW_RR_135'},
    {id:'valid',kind:'FVG_BEAR',side:-1,entry:89.71,stop:90.36,tp1:88.73,tp2:88.3,
     score:80,actionable:true,timeframe:'15m',low:89.7,high:89.8},
    {...fixtureBackup.candidates[0],entry:89.50,stop:88.85,tp1:90.5,tp2:91}
 ]};stateError=false;quote={price:89.54,ts:Date.now()};render();`);
 assert.match(text('state-title'),/SHORT .*@ 89.71/);
 assert.match(text('tactical-focus'),/ОЧІКУЄМО SHORT/);
 assert.equal(nodes.get('radar-primary').hidden,true);
 assert.match(text('zones'),/89.71.*90.36.*88.73.*Чекаємо підходу/);
 assert.doesNotMatch(text('state-title')+text('radar-primary')+text('zones'),/89\.48|89\.64|89\.67|0\.09R/);
 assert.equal(priceLines.length,0);
 assert.equal(run('positionBox(tacticalRadar().order)'),null);
 assert.equal(nodes.get('chart-legend').hidden,true);
 run('quote={price:89.59,ts:Date.now()};render();');
 assert.match(text('state-title'),/SHORT .*@ 89.71.*0.12/);
 assert.ok(!html.includes('Кандидати на вхід'));
 assert.ok(!html.includes('Зони сканера'));

 run(`state={...fixtureBackup,brain_config:{schema:1,missed_target_r:.75,browser_replace_atr:1,
   market_band_atr:.65,market_min_rr:1.35,limit_min_rr:1.40,risk_floor:.65,risk_cap:1.15,ttl_ms:5400000},
   orders:[{...orderFixture}],execution_audit:{closed_through_ts:Date.now()},
   dealing_range:{low:99.1,high:100.8}};syncBrain(state,true);
   quote={price:99.65,bid:99.64,ask:99.66,ts:Date.now()+1000};advanceBrain(quote,null,[]);render();`);
 assert.equal(run('displayOrders()[0].status'),'OPEN');
 assert.equal(run('state.orders[0].status'),'PENDING');
 assert.match(text('state-title'),/В УГОДІ LONG @ 99.66.*PnL/);
 assert.match(text('radar-primary'),/В УГОДІ/);
 const key=run('brainStorageKey(state.instrument)');
 const persisted=JSON.parse(storage.get(key));
 assert.equal(persisted.event.orderId,'limit-1');
 assert.equal(persisted.event.candleTs,Math.floor((now+1000)/60000)*60000);
 clock.value+=3000;
 run(`Object.assign(brain,{sourceTs:0,orders:[],event:null});syncBrain(state,true);
   quote={price:99.77,bid:99.76,ask:99.78,ts:Date.now()+1000};advanceBrain(quote,null,[]);render();`);
 assert.equal(run('displayOrders()[0].status'),'OPEN');
 assert.match(text('state-title'),/В УГОДІ LONG @ 99.66.*\+0.27R/);
 assert.equal(priceLines.length,6);
 assert.ok(priceLines.some(l=>l.title.startsWith('BSL')));
 assert.ok(priceLines.some(l=>l.title.startsWith('SSL')));
 assert.equal(nodes.get('chart-legend').hidden,false);
 run('state={...state,snapshot_ts:Date.now()+5000,orders:[{...state.orders[0],status:"OPEN",filled_ts:Date.now()+1000}]};syncBrain(state,false);render();');
 assert.equal(run('displayOrders()[0].local'),undefined);
 assert.equal(storage.has(key)?JSON.parse(storage.get(key)).event:null,null);

 clock.value+=7000;
 run(`state={...state,snapshot_ts:Date.now()+1000,orders:[],candidates:[],primary_bias:0};
   syncBrain(state,true);
   quote={price:89.54,bid:89.53,ask:89.55,ts:Date.now()+1500};
   const marketFixture={id:'fresh-market',actionable:true,market_now:true,order_type:'MARKET',
     side:-1,entry:89.53,stop:90.18,tp1:88.55,tp2:88.1,risk:.65,score:70};
   const lowReward={...marketFixture,id:'low-reward',tp1:89.48};
   advanceBrain(quote,null,[lowReward,marketFixture]);render();`);
 assert.equal(run('displayOrders()[0].id'),'fresh-market');
 clock.value+=3000;
 run('quote={price:88.85,bid:88.84,ask:88.86,ts:Date.now()+1500};advanceBrain(quote,null,[]);render();');
 assert.equal(run('displayOrders()[0].remaining'),.5);
 assert.equal(run('displayOrders()[0].stop'),89.43);
 assert.ok(priceLines.some(line=>line.price===89.43 && line.title.includes('ЗАХИЩЕНИЙ SL')));

 const copy=document.querySelectorAll('[data-focus]').find(n=>n.dataset.copyLabel==='Entry');
 await nodes.get('radar-primary').listeners.click({target:copy});
 assert.equal(clipboard,'89.53');

 clock.value+=4000;
 run(`globalThis.localStorage.removeItem(brainStorageKey(state.instrument));
   state={...state,snapshot_ts:Date.now(),orders:[],candidates:[]};syncBrain(state,true);
   const newLimit={id:'fresh-limit',order_type:'LIMIT',actionable:true,side:1,
     entry:89.40,stop:88.75,tp1:90.35,tp2:90.82,risk:.65,score:72};
   quote={price:89.54,bid:89.53,ask:89.55,ts:Date.now()+1500};
   advanceBrain(quote,null,[newLimit]);render();`);
 assert.equal(run('displayOrders()[0].status'),'PENDING');
 assert.equal(run('displayOrders()[0].entry'),89.40);
 assert.match(text('state-title'),/ЛІМІТ ВИСТАВЛЕНО LONG @ 89.40/);
 assert.equal( nodes.get('state-banner').dataset.mode,'PENDING');
 run(`state={...state,candidates:Array.from({length:4},(_,i)=>({...newLimit,id:'reserve-'+i,
   entry:89.28-i*.04,stop:88.63-i*.04,tp1:90.235-i*.04,tp2:90.8-i*.04}))};render();`);
 assert.equal(nodes.get('zones').children.length,3);

 // Check the canvas primitive, including draw order, both boxes and the pending marker.
 run(`history=Array.from({length:9},(_,i)=>({time:Math.floor(Date.now()/900000)*900-(8-i)*900,
    open:90,high:91,low:89,close:90}));currentBar=undefined;
   const marked={...orderFixture,status:'PENDING',entry:90.,stop:90.65,tp1:89.35,tp2:88.8,
     side:-1,risk:.65,placed_ts:Date.now()-15*60000,expires_ts:Date.now()+60000,
     zone:{kind:'SUPPLY_OB',timeframe:'15m',low:90.12,high:90.48,origin_ts:Date.now()-45*60000}};
   brain.orders=[marked];brain.sourceTs=state.snapshot_ts;render();`);
 const boxes=[], marks=[];
 const context={save(){},restore(){},beginPath(){},arc(...args){marks.push(args);},stroke(){},fill(){},
   fillRect(...args){boxes.push([this.fillStyle,...args]);},fillText(){}};
 const target={useMediaCoordinateSpace(cb){cb({context,mediaSize:{width:800,height:1800}});}};
 chartCalls.primitive.paneViews()[0].renderer().draw(target);
 assert.equal(boxes.length,3);
 assert.match(boxes[0][0],/167,176,191/);
 assert.match(boxes[1][0],/244,102,125/);
 assert.match(boxes[2][0],/47,202,179/);
 assert.ok(boxes[0][1]<boxes[1][1]);
 assert.equal(marks.length,1);
 run('brain.orders=[];render();');
 boxes.length=0;marks.length=0;
 chartCalls.primitive.paneViews()[0].renderer().draw(target);
 assert.equal(boxes.length,0);
 assert.equal(marks.length,0);
 assert.equal(priceLines.length,0);

 // Cold-start autonomous OB scan with the ledger unavailable.
 clock.value=Date.parse('2026-11-17T08:06:00Z');
 run(`globalThis.localStorage.removeItem(brainStorageKey(state.instrument));state=undefined;quote=undefined;
   bootstrapLiveBrain();render();`);
 const quarter=900000, closedAt=Math.floor(clock.value/quarter)*quarter;
 const m15=Array.from({length:120},(_,i)=>{
   const ts=closedAt-(120-i)*quarter;
   if(i===90) return [ts,89.84,91.45,89.70,89.83,100,0,0,'1'];
   if(i===109||i===110) return [ts,89.82,90.32,89.69,89.82,100,0,0,'1'];
   if(i===118) return [ts,89.88,89.95,89.64,89.71,100,0,0,'1'];
   if(i===119) return [ts,89.71,90.29,89.65,90.22,160,0,0,'1'];
   return [ts,89.82,90.00,89.69,89.82,100,0,0,'1'];
 });
 let last=90.30, ask=90.31, ts=clock.value;
 const candleCalls=[];
 ctx.fetch=async url=>{
   if(url.includes('/ticker')) return {ok:true,json:async()=>({code:'0',data:[{
     instId:'BZ-USDT-SWAP',last:String(last),askPx:String(ask),bidPx:String(ask-.02),ts:String(ts)
   }]})};
   if(url.includes('/candles?')) {
     const bar=new URL(url).searchParams.get('bar');
     candleCalls.push(bar);
     return {ok:true,json:async()=>({code:'0',data:bar==='15m'?m15:[]})};
   }
   return {ok:false,status:503};
 };
 await run('refreshLiveBrain()');
 assert.equal(run('state.snapshot_ts'),0);
 assert.equal(run('brain.orders.length'),0);
 assert.ok(run('brain.rows.some(row=>row.kind==="DEMAND_OB" && !row.activation_ready)'));
 assert.match(text('zones'),/Зона попиту/);
 assert.match(text('state-status'),/Real-Time 3s Active/);
 assert.equal(priceLines.length,0);
 clock.value+=3000;ts=clock.value;last=90.11;ask=90.12;
 await run('refreshLiveBrain()');
 assert.equal(run('brain.orders[0].status'),'PENDING');
 assert.equal(run('brain.orders[0].entry'),89.95);
 assert.equal(candleCalls.filter(b=>b==='5m').length,2);
 assert.equal(candleCalls.filter(b=>b==='15m').length,2);
 // The same structural OB goes immediately to MARKET when the ask is inside 0.18 ATR.
 clock.value+=3000;ts=clock.value;last=89.97;ask=89.98;
 run(`globalThis.localStorage.removeItem(brainStorageKey(state.instrument));state=undefined;quote=undefined;
   bootstrapLiveBrain();`);
 await run('refreshLiveBrain()');
 assert.equal(run('brain.orders[0].status'),'OPEN');
 assert.equal(run('brain.orders[0].order_type'),'MARKET');
 assert.equal(run('brain.orders[0].entry'),89.98);
 assert.equal(nodes.get('state-banner').dataset.positionState,'ENTERED');
 console.log('PASS: FLAT radar, PAPER storage, position boxes and autonomous MARKET/LIMIT without a ledger.');
})().catch(error=>{console.error(error);process.exitCode=1;});
