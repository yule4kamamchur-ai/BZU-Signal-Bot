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
 tp1:entry+side*.6,tp2:entry+side*1.1,reason:'ACCEPTED',reaction_eligible:true,valid_until:now+25*60000});
const long=zone('long',1,99.60,99.71,99.66),short=zone('short',-1,100.50,100.80,100.55),far=zone('far',-1,103,103.2,103.1);
const order={id:'limit-1',number:7,status:'PENDING',side:1,type:'LIMIT',setup:'MOMENTUM_STAIRCASE_15M',entry:99.66,stop:99.25,
 tp1:100.24,tp2:100.75,risk:.41,score:74,grade:'A',be_trigger:100.24,be_stop:99.68,tp1_fraction:.5,remaining:1,
 effective_ts:now-60000,expires_ts:now+42*60000,filled_ts:null};
const fixture={schema:1,cockpit_schema:2,primary_bias:0,min_structural_spread:.65,radar:{LONG:long,SHORT:short},instrument:'BZ-USDT-SWAP',tick:.01,snapshot_ts:now,status:'READY',price:99.84,orders:[],
 candidates:[long,short],zones:[long,short,far],recent_events:[{kind:'CANCELLED',side:1,entry:99.66,ts:now-60000,
 reason_text:'ціна відійшла без тесту'}],candles:{},daily:{entries:2,net_r:1.1},market_regime:'RANGE'};
ctx.fixture=fixture;ctx.orderFixture=order;
const cleanFixture=JSON.parse(JSON.stringify(fixture));
(async()=>{
 // Flush the initial failed fetch before assigning the synthetic state.
 await new Promise(resolve=>setImmediate(resolve));
 run('state=fixture;stateError=false;quote={price:99.84,ts:Date.now()};render();');
 assert.match(text('state-title'),/ЧЕКАЄМО ЗОНУ/);assert.equal(nodes.get('radar').children.length,2);
 assert.equal(run('visibleZones().length'),2);assert.match(text('last-event'),/Ліміт скасовано/);
 await nodes.get('all-zones').listeners.click();assert.equal(run('visibleZones().length'),3);
 await nodes.get('all-zones').listeners.click();
 const zoneButton=document.querySelectorAll('[data-focus]').find(x=>x.dataset.zone==='far');
 nodes.get('zones').listeners.click({target:zoneButton});assert.equal(run('selectedZone'),'far');
 assert.equal(run('visibleZones().length'),3);
 run('selectedZone=null;state.orders=[orderFixture];render();');
 assert.match(text('state-title'),/ЧЕКАЄМО ВХІД.*99.66/);assert.match(text('orders'),/41 хв|42 хв/);
 assert.ok(priceLines.some(l=>l.title.includes('LIMIT')));
 assert.match(text('chart-levels'),/LIMIT/);
 const copy=document.querySelectorAll('[data-focus]').find(x=>x.dataset.copyLabel==='Entry');
 await nodes.get('orders').listeners.click({target:copy});assert.equal(clipboard,'99.66');
 run('quote={price:99.71,ts:Date.now()+1};render();');assert.ok(nodes.get('state-banner').classList.contains('near'));
 run('quote={price:99.65,ts:Date.now()+2};render();');assert.match(text('state-detail'),/Дотик Entry/);
 assert.equal(nodes.get('state-banner').dataset.mode,'PENDING');
 run('state.orders=[{...orderFixture,status:"OPEN",filled_ts:Date.now()-1000}];quote={price:99.90,ts:Date.now()+3};render();');
 assert.equal(nodes.get('state-banner').dataset.mode,'ACTIVE');assert.match(text('orders'),/\+0.24/);
 run('quote={price:99.40,ts:Date.now()+4};render();');assert.equal(nodes.get('state-banner').dataset.mode,'DRAWDOWN');
 run('quote={price:100.25,ts:Date.now()+5};render();');assert.match(text('orders'),/Дотик TP1.*50%/);
 run('state.orders[0].tp1_hit=true;state.orders[0].remaining=.5;state.orders[0].pending_stop=99.68;state.orders[0].pending_stop_effective_ts=Date.now()+60000;render();');
 assert.match(text('orders'),/Запланований SL 99.68/);assert.doesNotMatch(text('orders'),/вже захищає/);
 run('state.orders[0].stop=99.68;render();');assert.match(text('orders'),/Стоп уже захищає вхід/);
 run('state.status="DATA_HOLD";render();');assert.equal(nodes.get('state-banner').dataset.mode,'DATA_HOLD');
 run('state.status="READY";state.orders=[{...orderFixture,status:"OPEN",side:-1,entry:100.15,stop:100.55,risk:.4,tp1:99.55,tp2:99.05,be_trigger:99.55,be_stop:100.13}];quote={price:99.9,ts:Date.now()+6};render();');
 assert.equal(nodes.get('state-banner').dataset.mode,'ACTIVE');assert.match(text('state-title'),/SHORT.*\+0.25/);
 assert.ok(priceLines.some(l=>l.title.includes('SL')&&l.title.includes('−1.00R')));
 run('state.snapshot_ts=Date.now()-28*60000;render();');assert.equal(nodes.get('state-banner').dataset.mode,'STALE');
 run('state.snapshot_ts=Date.now();quote={price:100.1,ts:Date.now()-20000};render();');assert.match(text('state-detail'),/за ціною зі зліпка/);
 run('state.orders=[];state.zones=[];state.candidates=[];state.recent_events=[];render();');assert.match(text('state-title'),/Скануємо ринок/);
 assert.match(text('orders'),/Скануємо ринок/);
 // UI fields use text nodes even when a public label contains markup.
 ctx.injection='<img src=x onerror=alert(1)>';run('state.zones=[{...fixture.zones[0],kind:injection}];render();');
 assert.match(text('zones'),/<img src=x/);assert.equal(nodes.get('zones').children[0].children[0].children[0].tagName,'STRONG');

 const reset=(side=1,overrides={},keepClock=false)=>{
   if(!keepClock)clock.value=Math.floor(clock.value/300000)*300000+60000;
   const entry=side===1?99.7:100.3;
   const row={id:'reaction-'+side,kind:side===1?'FVG_BULL':'FVG_BEAR',side,entry,low:entry-.05,high:entry+.05,
     stop:entry-side*.4,tp1:entry+side*.8,tp2:entry+side*1.2,score:75,score_source:'CONTEXT',status:'CANDIDATE',
     reaction_eligible:true,known_ts:clock.value-300000,valid_until:clock.value+1500000,...overrides};
   ctx.scenario={...cleanFixture,price:100,snapshot_ts:clock.value,orders:[],candidates:[row],zones:[row],
     primary_bias:side,primary_candidate_id:row.id,radar:{LONG:side===1?row:null,SHORT:side===-1?row:null},
     reaction_config:{retracement:.06,min_samples:3,max_sample_gap_ms:15000,ready_ttl_ms:15000,max_chase_r:.25}};
   run('state=scenario;stateError=false;quote=undefined;reactions.clear();previousLiveSample=null;m5Market=null;position.clear();document.hidden=false;');
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
   assert.match(text('state-title'),/LIVE РЕАКЦІЯ/);
   assert.equal(run('state.orders.length'),0); // A local signal is never a ledger/exchange fill.
   assert.equal(run('currentReaction().tracker.events.size'),1);
   feed(100-side*.22,1000);assert.equal(run('currentReaction().tracker.events.size'),1);
 }
 reset();feed(99.73);feed(99.70);feed(99.77);assert.notEqual(stage(),'READY'); // Started inside, no witnessed approach.
 reset();feed(100);feed(99.73);feed(99.70);feed(99.77,20000);assert.notEqual(stage(),'READY'); // Data gap revokes touch proof.
 reset();feed(100);feed(99.68);assert.equal(stage(),'INVALID');feed(100);assert.equal(stage(),'INVALID');
 assert.match(text('state-title'),/ЗАБЛОКОВАНО/);
 reset();feed(100);feed(99.73);feed(99.70);run('pauseReactions()');feed(99.77);assert.notEqual(stage(),'READY');
 reset();feed(100);feed(99.73);feed(99.70);feed(99.77);assert.equal(stage(),'READY');
 clock.value+=16000;run('render()');assert.equal(stage(),'MISSED');assert.doesNotMatch(text('state-title'),/LIVE РЕАКЦІЯ/);
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
 reset(1,{tp1:100.24});feed(100);feed(99.73);feed(99.70);feed(99.77);assert.equal(stage(),'MISSED');
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
 clock.value+=5000;run('render()');assert.equal(stage(),'MISSED');
 // A pending ledger order can be monitored but a local veto never changes its status.
 const pendingRow=reset();
 ctx.pending={...order,...pendingRow,id:'existing-limit',status:'PENDING',zone:{...pendingRow},expires_ts:clock.value+600000};
 run('state.orders=[pending]');feed(100);assert.equal(run('reactionRows().length'),1);
 feed(99.68);assert.equal(stage(),'INVALID');assert.equal(run('state.orders[0].status'),'PENDING');
 assert.match(text('orders'),/Локальний вхід заблоковано/);
 // Axis labels closer than 20 screen pixels are suppressed; exact levels remain in the text legend.
 reset();feed(100);const shown=priceLines.filter(x=>x.axisLabelVisible).map(x=>series.priceToCoordinate(x.price));
 for(let i=0;i<shown.length;i++)for(let j=i+1;j<shown.length;j++)assert.ok(Math.abs(shown[i]-shown[j])>=20);
 assert.match(text('chart-levels'),/SL/);
 console.log('PASS: 17 cockpit scenarios + 21 live-reaction/data-integrity scenarios; synthetic DOM/API adapter, no real browser rendering.');
})().catch(error=>{console.error(error);process.exitCode=1;});
