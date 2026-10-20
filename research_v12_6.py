"""Fixed chronological validation and final holdout on attributed evidence."""
from __future__ import annotations

import argparse
import copy
from dataclasses import asdict, replace
from pathlib import Path

import bot_oneshot as core
import full_limit_engine as engine
import research_evidence
import research_history
import research_validate as legacy_research
import setup_scanner
import signal_quality
import trade_dependencies


def boundaries(start,end,cfg):
    embargo=(cfg.ttl_minutes+cfg.max_hold_minutes)*16*60000+engine.TF['4H']
    width=end-start
    if width < 4*embargo+2*86400000:
        return {'status':'INSUFFICIENT_CHRONOLOGICAL_SPAN','available_ms':width,
                'required_ms':4*embargo+2*86400000,'embargo_ms':embargo,
                'live_entry_blocked':False}
    validation_start=start+width//2
    holdout_start=start+3*width//4
    return {'status':'READY','embargo_ms':embargo,
            'training':{'start':start,'end':validation_start-embargo},
            'validation':{'start':validation_start,'end':holdout_start-embargo},
            'holdout':{'start':holdout_start,'end':end}}


def summarize(report,state):
    trades=report['trades']
    report['by_timeframe']={tf:engine.statistics([t for t in trades if setup_scanner.timeframe(t)==tf])
                            for tf in setup_scanner.PRIMARY}
    report['by_formation_timeframe']={tf:engine.statistics([t for t in trades if t.get('formation_timeframe',setup_scanner.timeframe(t))==tf])
                                    for tf in ('3m',*setup_scanner.PRIMARY)}
    report['by_entry_route']={route:engine.statistics([t for t in trades if t.get('entry_route','NATIVE_PATTERN')==route])
                             for route in ('NATIVE_PATTERN','MICRO_PRECURSOR')}
    report['dependencies']=trade_dependencies.summary(trades,state.get('price_tick',.001))
    fill_pairs=[];win_pairs=[];errors=[];censored=0;queue_excluded=0
    by_id={t['id']:t for t in trades}
    for order in state.get('orders',[]):
        predicted=order.get('decision_forecast',{})
        if predicted.get('status')!='MODEL_ONLY':
            continue
        if predicted.get('eligibility_assumption') and order.get('capacity_waited'):
            queue_excluded+=1
            continue
        if order['status'] not in ('FILLED','CANCELLED','EXPIRED'):
            censored+=1;continue
        filled=order['status']=='FILLED'
        if predicted.get('fill_probability') is not None:
            fill_pairs.append((predicted['fill_probability'],float(filled)))
        if filled and order['id'] in by_id:
            trade=by_id[order['id']]
            if predicted.get('net_win_probability_given_fill') is not None:
                win_pairs.append((predicted['net_win_probability_given_fill'],float(trade['net_r']>0)))
            if predicted.get('expectancy_r_given_fill') is not None:
                errors.append(abs(predicted['expectancy_r_given_fill']-trade['net_r']))
    report['forecast_evaluation']={
        'fill_labels':len(fill_pairs),'fill_brier':sum((p-y)**2 for p,y in fill_pairs)/len(fill_pairs) if fill_pairs else None,
        'full_net_win_labels':len(win_pairs),
        'full_net_win_brier':sum((p-y)**2 for p,y in win_pairs)/len(win_pairs) if win_pairs else None,
        'net_r_mean_absolute_error':sum(errors)/len(errors) if errors else None,
        'censored_excluded':censored+len(engine.open_plans(state)),
        'queue_conditioned_excluded':queue_excluded,
        'prediction_time':'IMMUTABLE_PLACEMENT_DECISION_FORECAST',
        'actual_funding_in_labels':False,'forecast_carry_budget_included':True}
    return report


def validate(archive,cfg=None,evaluator=None):
    cfg=cfg or replace(engine.Config.from_env(),core=core)
    historical=archive.get('schema')==research_history.SCHEMA
    verified=research_history.verify(archive,engine) if historical else research_evidence.verify(archive,engine)
    result={'engine_version':engine.VERSION,'policy':signal_quality.STRATEGY,
            'policy_config':engine.config_dict(cfg),'data_provenance':verified,
            'goal_win_rate':.70,'win_rate_entry_gate_enabled':False,
            'timeframes':list(setup_scanner.PRIMARY),'parameter_search_performed':False,
            'holdout_used_for_learning':False,'production_state_written':False}
    if not historical and len(archive['quotes'])<2:
        return {**result,'status':'COLLECTING_CLEAN_EVIDENCE','observed_quotes':len(archive['quotes']),
                'live_entry_blocked':False,'by_timeframe':{tf:engine.statistics([]) for tf in setup_scanner.PRIMARY}}
    data=copy.deepcopy(archive['data']) if historical else research_evidence.dataset(archive,engine)
    result['execution_evidence_mode']='RECONSTRUCTED_HISTORICAL_QUOTES' if historical else 'OBSERVED_LIVE_QUOTES'
    cfg=replace(cfg,instrument=data['instrument'],tick=data['tick'])
    cfg.validate()
    result['policy_config']=engine.config_dict(cfg)
    split=boundaries(data['start_ts'],data['end_ts'],cfg)
    result['split']=split
    if split['status']!='READY':
        return {**result,'status':split['status'],'live_entry_blocked':False}
    result['data_quality']=legacy_research.audit_data(data)
    samples=list(legacy_research.snapshots(data))
    evaluator=evaluator or legacy_research.evaluate
    train=split['training']
    training,training_state=evaluator(engine,cfg,samples,train['start'],train['end'])
    observations=[copy.deepcopy(r) for r in training_state.get('quality',{}).get('observations',[])
                  if r['decision_ts']<r['closed_ts']<train['end']]
    fingerprint=signal_quality.digest(observations)
    phases={}
    for name in ('validation','holdout'):
        interval=split[name]
        evaluated,state=evaluator(engine,cfg,samples,interval['start'],interval['end'],observations,freeze=True)
        if signal_quality.digest(observations)!=fingerprint:
            raise ValueError('Evaluation modified frozen training evidence')
        phases[name]=summarize(evaluated,state)
        phases[name]['frozen_training_sha256']=fingerprint
        phases[name]['training_labels_before_ts']=train['end']
    model={'schema':signal_quality.SCHEMA,'strategy':signal_quality.STRATEGY,
           'signature':signal_quality.signature(cfg),'trained_until_ts':train['end'],
           'observations':observations,'settings':asdict(signal_quality.Settings())}
    model['artifact_id']=signal_quality.digest(model)
    return {**result,'status':'HELD_OUT_EVALUATION_COMPLETE','training':summarize(training,training_state),
            **phases,'model':model,
            'limitations':['OHLC signal execution, not exchange account fills',
                           'Fixed policy; validation and holdout never tune gates or thresholds',
                           'Actual funding/queue/market impact are unavailable',
                           'Dependency groups are risk episodes, not a proof of statistical independence']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive',type=Path,default=Path('research_market_v12_6.json'))
    parser.add_argument('--output',type=Path,default=Path('research_v12_6_output'))
    parser.add_argument('--download',action='store_true',help='Download a separate controlled HTTP history bundle')
    parser.add_argument('--days',type=int,default=90)
    parser.add_argument('--instrument',default=core.OKX_INST_ID)
    args=parser.parse_args()
    args.output.mkdir(parents=True,exist_ok=True)
    if args.download:
        import time
        archive=research_history.download(args.instrument,args.days,int(time.time()*1000))
        engine.atomic_write(args.output/'controlled_history.json',archive)
    else:
        archive=engine.read_json(args.archive)
    if not archive:
        raise ValueError('Initialize a clean archive or collect verified live snapshots first')
    report=validate(archive)
    args.output.mkdir(parents=True,exist_ok=True)
    model=report.pop('model',None)
    engine.atomic_write(args.output/'validation.json',report)
    if model:
        engine.atomic_write(args.output/'quality_model.json',model)
    print(report['status'])


if __name__=='__main__':
    main()
