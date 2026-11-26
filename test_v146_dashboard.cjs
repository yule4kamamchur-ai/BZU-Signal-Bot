/* Offline UI state-contract checks using a minimal DOM adapter. No network or browser.
 * Run: node test_v146_dashboard.cjs
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
const chartCalls={data:[],text:[]};let clipboard='';
const clock={value:Date.parse('2026-11-17T08:01:00Z')};
class TestDate extends Date {static now(){return clock.value;}}
const storage=new Map();
const ctx={localStorage:{getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value),
  removeItem:key=>storage.delete(key)},document,window:{innerWidth:1440},navigator:{clipboard:{writeText:async text=>{clipboard=text;}}},
  Intl,URLSearchParams,AbortSignal,Date:TestDate,console,setInterval(){},clearInterval(){},WebSocket:function(){},
  fetch:async()=>({ok:false,status:503,json:async()=>({})})};
const series={setData(data){chartCalls.data=data;},update(bar){chartCalls.lastBar=bar;},applyOptions(){},priceToCoordinate:p=>400-(p-98)*100,
  attachPrimitive(primitive){primitive.attached({requestUpdate(){}});chartCalls.primitive=primitive;}};
ctx.window.LightweightCharts={CandlestickSeries:{},LineStyle:{Dashed:2,Solid:0},createChart(){return {addSeries:(_type,options)=>{
  chartCalls.seriesOptions=options;return series;},
  timeScale:()=>({fitContent(){},timeToCoordinate:t=>200+(t-clock.value/1000)/30}),priceScale:()=>({applyOptions(){}})};}};
vm.createContext(ctx);vm.runInContext(fs.readFileSync('live_terminal.js','utf8'),ctx);
const run=source=>vm.runInContext(source,ctx),text=id=>nodes.get(id).textContent;
const find=(node,name)=>node.classList.contains(name)?node:node.children.map(child=>find(child,name)).find(Boolean);
const radar=nodes.get('radar-primary'), banner=nodes.get('state-banner');
const axisViews=()=>chartCalls.primitive.priceAxisViews();
const tone=()=>['radar-primary','state-banner','tactical-focus'].map(id=>nodes.get(id).dataset.pnlTone);
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
 run('state=undefined; stateError=false; renderReleaseStatus()');
 assert.match(text('server-release'), /не підтверджено/);
 run('state={...fixtureBackup,version:"v14.4.5"}; renderReleaseStatus()');
 assert.match(text('server-release'), /v14\.4\.5 · версії не збігаються/);
 run('state={...fixtureBackup,version:"v14.4.6"}; renderReleaseStatus()');
 assert.equal(text('server-release'), 'Сервер: v14.4.6 · зліпок отримано');
 run('state.snapshot_ts=Date.now()-28*60_000; renderReleaseStatus()');
 assert.match(text('server-release'), /застарілий/);
 run('state.snapshot_ts=Date.now(); stateError=true; renderReleaseStatus()');
 assert.match(text('server-release'), /недоступний/);
 run('state={...fixtureBackup,version:"v14.4.5"}; stateError=false;');
 ctx.fetch=async url=>({ok:true,json:async()=>({...cleanFixture,version:'v14.4.6'})});

 await run('pollState()');

 assert.equal(run('state.version'),'v14.4.6');
 assert.equal(text('server-release'),'Сервер: v14.4.6 · зліпок отримано');
 console.log('PASS: 6 dashboard release scenarios, including same-timestamp server upgrade');
})().catch(error=>{console.error(error);process.exitCode=1;});
