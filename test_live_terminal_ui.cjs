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
const chartCalls={data:[],text:[]};let clipboard='';
const clock={value:Date.parse('2026-11-17T08:01:00Z')};
class TestDate extends Date {static now(){return clock.value;}}
const storage=new Map();
const ctx={localStorage:{getItem:key=>storage.get(key),setItem:(key,value)=>storage.set(key,value),
  removeItem:key=>storage.delete(key)},document,window:{innerWidth:1440},navigator:{clipboard:{writeText:async text=>{clipboard=text;}}},
  Intl,URLSearchParams,AbortSignal,Date:TestDate,console,setInterval(){},clearInterval(){},WebSocket:function(){},
  fetch:async()=>({ok:false,status:503,json:async()=>({})})};
const series={setData(data){chartCalls.data=data;},update(){},applyOptions(){},priceToCoordinate:p=>400-(p-98)*100,
  attachPrimitive(primitive){primitive.attached({requestUpdate(){}});chartCalls.primitive=primitive;}};
ctx.window.LightweightCharts={CandlestickSeries:{},LineStyle:{Dashed:2,Solid:0},createChart(){return {addSeries:()=>series,
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
 assert.equal(radar.hidden,true);
 assert.deepEqual(tone(),['neutral','neutral','neutral']);
 assert.match(text('zones'),/89.71.*90.36.*88.73.*Чекаємо підходу/);
 assert.doesNotMatch(text('state-title')+text('radar-primary')+text('zones'),/89\.48|89\.64|89\.67|0\.09R/);
 assert.equal(axisViews().length,0);
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
 assert.equal(axisViews().length,6);
 assert.equal(axisViews()[0].text(),'99.66');
 assert.equal(axisViews()[0].backColor(),'#303341');
 assert.ok(axisViews().every(view=>view.visible()&&!view.tickVisible()));
 assert.equal(find(radar,'target-r-badge').textContent,'+1.41R');
 assert.match(text('chart-levels'),/BSL @ 100\.82.*SSL @ 99\.16/);
 assert.deepEqual(tone(),['profit','profit','profit']);
 assert.match(find(radar,'pnl-badge').textContent,/\+0\.27R/);

 // A live move from profit to loss uses one PnL for all three surfaces.
 run('quote={price:99.55,ts:Date.now()};render();');
 assert.deepEqual(tone(),['loss','loss','loss']);
 assert.equal(find(radar,'pnl-badge').textContent,'−0.27R');
 assert.equal(find(radar,'pnl-unit').textContent,'−0.11 $/од.');
 assert.match(find(radar,'rr-endpoints').textContent,/До SL · 0\.30 \$.*До TP1 · 0\.69 \$/);
 assert.equal(find(radar,'rr-progress-track').getAttribute('aria-valuenow'),'30');
 assert.equal(Math.round(parseFloat(find(radar,'rr-progress-fill').style.clipPath.split(' ')[1])),70);
 assert.equal(Math.round(parseFloat(find(radar,'rr-entry-marker').style.left)),41);
 run('quote={price:99.66,ts:Date.now()};render();');
 assert.deepEqual(tone(),['profit','profit','profit']);
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
 assert.ok(axisViews().some(view=>view.text()==='89.43' && view.visible()));
 assert.match(text('chart-levels'),/ЗАХИЩЕНИЙ SL/);
 assert.match(find(radar,'scale-checkpoint').textContent,/✓ .*50.*Б\/У/);
 assert.match(find(radar,'rr-endpoints').textContent,/До SL · 0\.58 \$.*До TP2 · 0\.75 \$/);
 assert.equal(find(radar,'rr-progress-track').getAttribute('aria-valuenow'),'44');
 assert.equal(find(radar,'rr-entry-marker'),undefined);

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
 assert.equal(banner.dataset.mode,'PENDING');
 assert.deepEqual(tone(),['neutral','neutral','neutral']);
 assert.equal(find(radar,'telemetry-pnl'),undefined);
 assert.deepEqual(find(radar,'tactical-metrics').children.map(node=>node.dataset.level),['entry','stop','tp1','tp2']);
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
 const boxes=[], marks=[], labels=[], outlines=[], dashes=[], rayStarts=[], rayEnds=[], strokes=[];
 const context={save(){},restore(){},beginPath(){},arc(...args){marks.push(args);},fill(){},
   stroke(){strokes.push([this.strokeStyle,this.lineWidth]);},
   strokeRect(){outlines.push([this.strokeStyle,this.lineWidth]);},
   fillRect(...args){boxes.push([this.fillStyle,...args]);},fillText(...args){labels.push(args);},
   measureText:text=>({width:text.length*6}),setLineDash:pattern=>dashes.push([...pattern]),
   moveTo:(...args)=>rayStarts.push(args),lineTo:(...args)=>rayEnds.push(args)};
 const target={
   useMediaCoordinateSpace(cb){cb({context,mediaSize:{width:800,height:1800}});},
   useBitmapCoordinateSpace(cb){cb({context,bitmapSize:{width:1600,height:3600},horizontalPixelRatio:2,verticalPixelRatio:2});},
 };
 chartCalls.primitive.paneViews()[0].renderer().draw(target);
 const areas=boxes.filter(([color])=>color!=='rgba(18,21,33,.88)');
 assert.equal(areas.length,3);
 assert.match(areas[0][0],/167,176,191/);
 assert.equal(areas[1][0],'rgba(244,63,94,.08)');
 assert.equal(areas[2][0],'rgba(16,185,129,.08)');
 assert.deepEqual(outlines,[['rgba(244,63,94,.22)',1],['rgba(16,185,129,.22)',1]]);
 assert.ok(areas[0][1]<areas[1][1]);
 assert.equal(marks.length,1);
 assert.equal(labels.filter(([label])=>label.startsWith('POI')).length,1);
 assert.ok(labels.filter(([label])=>!label.startsWith('POI')).every(([label,x])=>label.length<=5 && x>=750));
 assert.ok(dashes.some(pattern=>pattern.join(',')==='2,14'));
 assert.ok(dashes.some(pattern=>pattern.join(',')==='2,6'));
 assert.ok(dashes.some(pattern=>pattern.join(',')==='8,8'));
 assert.ok(strokes.some(([color])=>color==='rgba(148,163,184,0.38)'));
 assert.ok(strokes.every(([,width])=>width===1));
 assert.deepEqual(rayStarts[0], [Math.round(marks[0][0]*2), rayEnds[0][1]]);
 assert.equal(rayEnds[0][0],1600);
 run('brain.orders=[];render();');
 boxes.length=0;marks.length=0;labels.length=0;
 chartCalls.primitive.paneViews()[0].renderer().draw(target);
 assert.equal(boxes.length,0);
 assert.equal(marks.length,0);
 assert.equal(labels.length,0);
 assert.equal(axisViews().length,0);

 run(`state={...state,recent_events:[
   {kind:'CANCELLED',ts:Date.now()},{kind:'PLACED',ts:Date.now()},{kind:'FILLED',ts:Date.now()}
 ]};render();`);
 assert.deepEqual(nodes.get('events').children.map(node=>node.dataset.eventTone),['entry','order','inactive']);

 const flashes=[];
 let cancelled=0;
 nodes.get('price').animate=(frames,ms)=>{flashes.push([frames[0].backgroundColor,ms]);return {cancel:()=>cancelled++};};
 run(`quote=undefined;
   const ticks=(last,offset)=>acceptTicker({instId:state.instrument,last,ts:Date.now()+offset});
   ticks(90,0);ticks(90.01,1000);ticks(90.01,1000);ticks(90,2000);ticks(89.99,1500);
   window.matchMedia=()=>({matches:true});ticks(89.98,2500);`);
 assert.deepEqual(flashes,[['rgba(16,185,129,.25)',300],['rgba(239,68,68,.25)',300]]);
 assert.equal(cancelled,1);

 // The autonomous browser applies the same sequential liquidity guard and front-run proof.
 run(`const at=Math.floor(Date.now()/300000)*300000, step5=300000;
   for (const side of [1,-1]) {
     const b=(ts,o,h,l,c)=>({ts,open:side===1?o:200-o,high:side===1?h:200-l,
       low:side===1?l:200-h,close:side===1?c:200-c,volume:100});
     const before=[.1,.15,.4,.2,.1].map((h,i)=>b(at-(5-i)*step5,100.,100.+h,99.95,100.05));
     const after=[b(at,100.,100.8,99.95,100.7),b(at+step5,100.7,100.72,99.98,100.2)];
     brain.series={'15m':[],'1m':[],'5m':[...before,...after]};
     const position={...orderFixture,status:'OPEN',entry:100.,side,risk:1.,
       stop:100.-side,filled_ts:at,tp1_hit:false};
     brainProtectLiquidity(position,{ts:at+2*step5},100.+side*.2);
     if (Math.abs(position.stop-(100.+side*.05))>1e-8) throw new Error('Liquidity stop missing');
     const bare={...position,stop:100.-side,liquidity_protection:null};
     brain.series['5m']=after;
     brainProtectLiquidity(bare,{ts:at+2*step5},100.+side*.2);
     if (bare.stop!==100.-side) throw new Error('Numeric profit alone armed stop');
     const first=b(at,100.25,100.30,100.10,100.15);
     const last=b(at+step5,100.15,100.65,100.14,100.62);
     const pending={...orderFixture,side,entry:100.,risk:1.,stop:100.-side,placed_ts:at,
       tp1:100.+side*3.,tp2:100.+side*4.,qualification_tp1:100.+side*3.};
     brain.series={'5m':[first,last],'1m':Array.from({length:10},(_,i)=>
       b(at+i*60000,100.25,100.30,100.10,100.25))};
     state.brain_config={...state.brain_config,risk_floor:.65,risk_cap:2.};
     const live={ts:at+2*step5,ask:side===1?100.64:99.38,bid:side===1?100.62:99.36};
     if (!brainFrontRun(pending,live,{atr:.5})) throw new Error('Front-run proof missing');
     brain.series['1m'][0]=b(at,100.25,100.30,99.99,100.25);
     if (brainFrontRun(pending,live,{atr:.5})) throw new Error('Filled limit replaced');
   }`);
 // Two closed M15 breaches permit a structural exit only after six quarters.
 run(`const known=Math.floor(Date.now()/brainQuarter)*brainQuarter;
   for(const side of [1,-1]) {
     const term=(x)=>100+side*x;
     const history=Array.from({length:120},(_,i)=>{
       const down=i===118?-.40:i===119?-.50:0;
       const wick=i===114?-.20:down-.05;
       return {ts:known-(120-i)*brainQuarter,open:term(down),close:term(down),
         high:side===1?term(down+.05):term(wick),low:side===1?term(wick):term(down+.05),volume:100};
     });
     brain.series={'15m':history,'1m':[]};
     const held={side,filled_ts:known-6*brainQuarter,tp1_hit:false};
     if (!brainAdverseClosePair(held,{ts:known},term(-.51))) throw new Error('Closed M15 pair missed');
     if (brainAdverseClosePair(held,{ts:known-1},term(-.51))) throw new Error('Exit before six quarters');
     if (brainAdverseClosePair(held,{ts:known},term(-.10))) throw new Error('Recovered structure still closed');
   }`);
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
 let last=90.30, ask=90.31, ts=clock.value, m1=[];
 const candleCalls=[];
 ctx.fetch=async url=>{
   if(url.includes('/ticker')) return {ok:true,json:async()=>({code:'0',data:[{
     instId:'BZ-USDT-SWAP',last:String(last),askPx:String(ask),bidPx:String(ask-.02),ts:String(ts)
   }]})};
   if(url.includes('/candles?')) {
     const bar=new URL(url).searchParams.get('bar');
     candleCalls.push(bar);
     return {ok:true,json:async()=>({code:'0',data:bar==='15m'?m15:bar==='1m'?m1:[]})};
   }
   return {ok:false,status:503};
 };
 await run('refreshLiveBrain()');
 assert.equal(run('state.snapshot_ts'),0);
 assert.equal(run('brain.orders.length'),0);
 assert.ok(run('brain.rows.some(row=>row.kind==="DEMAND_OB" && !row.activation_ready)'));
 assert.match(text('zones'),/Зона попиту/);
 assert.match(text('state-status'),/Real-Time 3s Active/);
 assert.equal(axisViews().length,0);
 clock.value+=3000;ts=clock.value;last=90.11;ask=90.12;
 await run('refreshLiveBrain()');
 assert.equal(run('brain.orders[0].status'),'PENDING');
 assert.equal(run('brain.orders[0].entry'),89.95);
 assert.equal(candleCalls.filter(b=>b==='5m').length,2);
 assert.equal(candleCalls.filter(b=>b==='15m').length,2);
 // Inside the touch band, a passive order still rests without a closed rejection.
 clock.value+=3000;ts=clock.value;last=89.97;ask=89.98;
 const clearPaper=`globalThis.localStorage.removeItem(brainStorageKey(state.instrument));
   state=undefined;quote=undefined;bootstrapLiveBrain();`;
 run(clearPaper);
 await run('refreshLiveBrain()');
 assert.equal(run('brain.orders[0].status'),'PENDING');
 assert.equal(run('brain.orders[0].entry'),89.95);
 // Supply a new completed M1 wick and direction-aligned close.
 const m1Close=Math.floor(clock.value/60000)*60000+60000;
 clock.value=m1Close;ts=clock.value;
 m1=[[m1Close-60000,89.95,90.00,89.91,89.98,100,0,0,'1']];
 run(clearPaper);
 await run('refreshLiveBrain()');
 assert.equal(run('brain.orders[0].status'),'OPEN');
 assert.equal(run('brain.orders[0].order_type'),'MARKET');
 assert.equal(run('brain.orders[0].entry'),89.98);
 assert.equal(nodes.get('state-banner').dataset.positionState,'ENTERED');
 console.log('PASS: FLAT radar, PAPER execution and storage, position rays, live telemetry, ticker and ledger.');
})().catch(error=>{console.error(error);process.exitCode=1;});
