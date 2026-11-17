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
  timeScale:()=>({fitContent(){},timeToCoordinate:()=>0}),priceScale:()=>({applyOptions(){}})};}};
vm.createContext(ctx);vm.runInContext(fs.readFileSync('live_terminal.js','utf8'),ctx);
const run=source=>vm.runInContext(source,ctx),text=id=>nodes.get(id).textContent;
const now=clock.value;
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
ctx.fixtureBackup=cleanFixture;

(async()=>{
 await new Promise(resolve=>setImmediate(resolve));
 run(`state={...fixtureBackup,primary_bias:-1,price:89.54,candidates:[
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
 assert.match(text('state-title'),/SHORT @ 89.71/);
 assert.match(text('tactical-focus'),/ОЧІКУЄМО SHORT/);
 assert.match(text('radar-primary'),/89.71.*90.36.*88.73/);
 assert.doesNotMatch(text('state-title')+text('radar-primary')+text('zones'),/89\.48|89\.64|89\.67|0\.09R/);
 assert.deepEqual(priceLines.map(line=>line.price).sort((a,b)=>a-b),[88.3,88.73,89.71,90.36]);
 assert.equal(run('visibleZones()[0].id'),'valid');
 run('quote={price:89.59,ts:Date.now()};render();');
 assert.match(text('state-title'),/SHORT @ 89.71.*0.12/);
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
 assert.equal(priceLines.length,4);
 run('state={...state,snapshot_ts:Date.now()+5000,orders:[{...state.orders[0],status:"OPEN",filled_ts:Date.now()+1000}]};syncBrain(state,false);render();');
 assert.equal(run('displayOrders()[0].local'),undefined);
 assert.equal(storage.has(key),false);

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
 console.log('PASS: tactical radar, structural entry, R:R, browser reload, MARKET entry and protected stop.');
})().catch(error=>{console.error(error);process.exitCode=1;});
