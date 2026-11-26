// Execute the actual snapshot loader in a minimal sandbox, without a browser/network.
const vm=require('node:vm'),fs=require('node:fs'),assert=require('node:assert/strict');
const source=fs.readFileSync('multi_scanner.js','utf8').replace(/\}\)\(\);\s*$/,`globalThis.__test={loadScannerState,latest:()=>latestData,source:()=>snapshotSource,failed:()=>scannerFetchError}; checkSlotAlerts=()=>{}; renderAll=()=>{}; renderMeta=()=>{}; })();`);
let pages={updated_ts:1000,instruments:[]},raw={updated_ts:2000,instruments:[]},failRaw=false,failPages=false;
const context={document:{readyState:'loading',addEventListener(){},querySelector(){return {content:'https://raw.githubusercontent.com/test/bot/main/multi_scanner_state.json'}},getElementById(){return null}},Date,AbortController,setTimeout,clearTimeout,console,fetch:async url=>{
 const isRaw=url.startsWith('https://raw.'); if(isRaw?failRaw:failPages)throw new Error('offline');
 return {ok:true,json:async()=>JSON.parse(JSON.stringify(isRaw?raw:pages))};
}};
vm.createContext(context);vm.runInContext(source,context);
(async()=>{
 const t=context.__test;await t.loadScannerState();assert.equal(t.latest().updated_ts,2000);assert.equal(t.source(),'GitHub state');
 raw={updated_ts:2000,last_attempt_ts:3000,health:{status:'FAILED_DATA'},instruments:[]};pages={updated_ts:2000,instruments:[]};await t.loadScannerState();assert.equal(t.latest().last_attempt_ts,3000);
 failRaw=true;pages={updated_ts:4000,instruments:[]};await t.loadScannerState();assert.equal(t.latest().updated_ts,4000);assert.equal(t.source(),'Pages / worker');assert.equal(t.failed(),false);
 failPages=true;await t.loadScannerState();assert.equal(t.latest().updated_ts,4000);assert.equal(t.failed(),true);
 console.log('4 dashboard snapshot regressions passed');
})().catch(e=>{console.error(e);process.exitCode=1});
