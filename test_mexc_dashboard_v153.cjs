// DOM-free render checks of the actual v15.3 dashboard functions.
const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
const source=fs.readFileSync('multi_scanner.js','utf8').replace(/\}\)\(\);\s*$/,`globalThis.__test={directionPanel,renderDualSlotsAndDaily,getClosedHistoryRows,checkSlotAlerts,setData:(d)=>{latestData=d}}; })();`);
const els={}; const el=id=>els[id]||(els[id]={hidden:false,textContent:'',innerHTML:'',className:'',classList:{add(){},remove(){},toggle(){}}});
const context={document:{readyState:'loading',addEventListener(){},querySelector(){return null},getElementById:el},console,Date};
vm.createContext(context);vm.runInContext(source,context);
const t=context.__test;
const panel=t.directionPanel({regime:'TRANSITION_DOWN',side:-1,status:'WATCH',local_wave:{m5:{side:-1},m15:{side:-1}},relative_strength:{available:true,relative_atr:-1.1,side:-1},trigger_tf:'M5',trigger_offset:1,signal_age_sec:420,rejection_codes:['PORTFOLIO_BUDGET'],direction_diagnostics:{'1':{detected:0,ready:0,windows_checked:[{tf:'M5'},{tf:'M5'}]},'-1':{detected:2,ready:0,windows_checked:[{tf:'M5'},{tf:'M5'}],rejections:['CHASE_DISTANCE','DELAYED_DATA_GAP']}}});
assert.match(panel,/LONG/);assert.match(panel,/SHORT/);assert.match(panel,/M5 2\/2/);assert.match(panel,/420 с/);assert.match(panel,/ціна вже відійшла/);assert.match(panel,/спільний бюджет вичерпано/);
assert.equal(t.directionPanel({}), '');
const active={symbol:'XPT_USDT',label:'XPT',side:-1,entry:100,stop:101,tp:98,risk:1,tick:.01,current_price:99,sizing:{risk_usd:1}};
const d={oil_slot:{daily_all:{trades:1,net_r:1,net_usd:1}},cm_slot:{daily_all:{trades:2,net_r:-1,net_usd:-1}},metal_slot:{active,daily_all:{trades:3,net_r:2,net_usd:2},history:[{symbol:'XPT_USDT',closed_ts:1}]},portfolio:{used_risk_usd:1,risk_usd:3,used_notional_usd:100,notional_usd:500,per_slot_risk_cap:1,status:'OK'}};
t.setData(d);t.renderDualSlotsAndDaily();assert.equal(el('ms-slot-metal').hidden,false);assert.match(el('ms-slot-metal-title').textContent,/SHORT/);assert.match(el('ms-day-total').textContent,/6 уг/);assert.match(el('ms-portfolio').textContent,/3.00/);assert.equal(t.getClosedHistoryRows()[0].symbol,'XPT_USDT');t.checkSlotAlerts(d);
t.setData({oil_slot:{},cm_slot:{}});t.renderDualSlotsAndDaily();assert.equal(el('ms-slot-metal').hidden,true);
console.log('3 v15.3 dashboard render regressions passed');
