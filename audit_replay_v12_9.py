import sys,json,collections,copy,contextlib,io,time,hashlib
from pathlib import Path
from dataclasses import replace
root=Path(sys.argv[1]);sys.path.insert(0,str(root))
import full_limit_engine as e,bot_oneshot as core,research_evidence as evidence,research_validate as rv,trade_dependencies
raw=json.load(open(root/'research_market_v12_6.json'));data=evidence.dataset(raw,e);samples=list(rv.snapshots(data));cfg=replace(e.Config.from_env(),core=core,tick=data['tick']);state=e.new_state(cfg)
rows=[];started=time.time();code_hashes={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.glob('*.py')}
for i,s in enumerate(samples):
 with contextlib.redirect_stdout(io.StringIO()):r=e.run_cycle(state,s,cfg)
 rows.append({'ts':s.now,'price':s.price,'action':r['action'],'health':r.get('analysis',{}).get('health'),
  'placed':[{'id':p['id'],'side':p['side'],'entry':p['entry'],'setup':p['setup'],'tf':p.get('setup_timeframe'),'formation':p.get('formation_timeframe'),'route':p.get('entry_route')} for p in r.get('selected_plans',[])],
  'admission':r.get('analysis',{}).get('admission_by_side',{}),'funnel':r.get('analysis',{}).get('funnel',{})})
 if i%25==0:print(i,e.iso(s.now),'placed',sum(len(x['placed']) for x in rows),'trades',len(state['trades']),flush=True)
summary={'version':e.VERSION,'mode':'EXPLORATORY_READ_ONLY_FIRST_SEEN_REPLAY_NOT_HOLDOUT','production_state_written':False,'code_sha256':code_hashes,'configuration':e.config_dict(cfg),
 'observations':len(samples),'first':e.iso(samples[0].now),'last':e.iso(samples[-1].now),'evidence_sha256':raw['content_sha256'],
 'same_window_used_for_bug_diagnosis':True,'initial_portfolio':'EMPTY_SEPARATE_FROM_ACTUAL_HISTORY',
 'statistics':e.statistics(state['trades']),'dependencies':trade_dependencies.summary(state['trades'],cfg.tick),
 'by_timeframe':{tf:e.statistics([t for t in state['trades'] if t.get('setup_timeframe')==tf]) for tf in ('15m','1H','4H')},
 'by_formation_timeframe':{tf:e.statistics([t for t in state['trades'] if t.get('formation_timeframe',t.get('setup_timeframe'))==tf]) for tf in ('3m','15m','1H','4H')},
 'by_side':{str(side):e.statistics([t for t in state['trades'] if t['side']==side]) for side in (-1,1)},
 'orders':[{k:o.get(k) for k in ('id','side','entry','setup','setup_timeframe','formation_timeframe','entry_route','placed_ts','resolved_ts','status','reason')} for o in state['orders']],
 'trades':[{k:t.get(k) for k in ('id','side','entry','setup','setup_timeframe','formation_timeframe','entry_route','filled_ts','closed_ts','net_r','close_reason')} for t in state['trades']],
 'open_at_end':[{k:p.get(k) for k in ('id','side','entry','setup','status')} for p in e.open_plans(state)],'rows':rows,'elapsed_seconds':time.time()-started}
Path(sys.argv[2]).write_text(json.dumps(summary,ensure_ascii=False,indent=2))
print(json.dumps({k:v for k,v in summary.items() if k in ('version','statistics','by_side','by_timeframe','elapsed_seconds')},ensure_ascii=False))
