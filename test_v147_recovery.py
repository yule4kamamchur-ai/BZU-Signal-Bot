"""Targeted offline tests for v14.4.7 recovery and resumable replay."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import Mock,patch
import adaptive_entry as timing
import calibration_storage as storage
import calibrate_meta_model as calibrate
import full_limit_engine as engine
import bot_oneshot as core
import test_v146_adaptive_entry as fixtures

M,NOW=fixtures.M,fixtures.NOW

class RecoveryTests(TestCase):
    cfg=fixtures.AdaptiveEntryTests.cfg
    def fixture(self):
        return fixtures.AdaptiveEntryTests()

    def delayed(self,side=1,missing=False,invalid=False,gap=10*M):
        s=self.fixture().snapshot(side,witness=True)
        def c(ts):
            o,h,l,close=100.06,100.08,100.04,100.06
            if invalid and ts==NOW+2*M: l,close=99.70,99.80
            if side==-1: o,h,l,close=200-o,200-l,200-h,200-close
            return engine.Candle(ts,o,h,l,close,100,True)
        minutes=s.candles['1m']+[c(NOW+i*M) for i in range(6) if not (missing and i==2)]
        px=100.06 if side==1 else 99.94
        return replace(s,now=NOW+6*M,ticker_ts=NOW+6*M,price=px,bid=px-.005,ask=px+.005,
                       candles={**s.candles,'1m':minutes},metadata={'scan_gap_ms':gap})

    def proof(self,s,side,tf):
        return timing.trigger(s,side,self.fixture().zone(side),100.,1.,self.cfg,engine,tf)

    def test_recover_m1_m3_m5_long_short(self):
        for side in (1,-1):
            for tf in timing.STEPS:
                with self.subTest(side=side,tf=tf):
                    p=self.proof(self.delayed(side),side,tf)
                    self.assertIsNotNone(p)
                    self.assertEqual(p['known_ts'],NOW)
                    self.assertTrue(p['recovered_between_scans'])
                    self.assertEqual(p['confirmation_age_ms'],6*M)

    def test_no_gap_no_old_trigger(self):
        for tf in timing.STEPS:
            self.assertIsNone(self.proof(self.delayed(gap=0),1,tf))

    def test_recovery_requires_intact_path(self):
        for tf in timing.STEPS:
            for s in (self.delayed(missing=True),self.delayed(invalid=True),replace(self.delayed(),price=100.5,ask=100.5)):
                self.assertIsNone(self.proof(s,1,tf))

    def test_recovery_has_maximum_age(self):
        s=self.delayed(gap=60*M)
        s=replace(s,now=NOW+21*M,ticker_ts=NOW+21*M)
        self.assertIsNone(self.proof(s,1,'1m'))

    def test_neutral_h1_requires_affirmative_alignment(self):
        for side in (1,-1):
            s=self.fixture().snapshot(side);z=self.fixture().zone(side)
            velocity=timing.approach(s,side,z,100.,.01,engine)
            good={'side_4h':side,'side_1h':0,'trend_anchor_side':side}
            self.assertTrue(timing.early_allowed(s,side,100.,z,good,{},velocity,.01,engine))
            self.assertFalse(timing.early_allowed(s,side,100.,z,{'side_4h':side,'side_1h':0},{},velocity,.01,engine))
            self.assertFalse(timing.early_allowed(s,side,100.,z,{**good,'side_1h':-side},{},velocity,.01,engine))
            self.assertFalse(timing.early_allowed(s,side,100.,z,good,{},dict(velocity,calm=False),.01,engine))

    def test_counter_m5_has_extra_evidence_and_shock_falls_back(self):
        for side in (1,-1):
            s=self.fixture().snapshot(side,witness=True)
            regime={'side_4h':-side,'side_1h':side};vel={'micro_available':True,'blocked':False}
            proof={'confirmation_timeframe':'5m','micro_shift':True,'rvol':1.3,'body_atr':.8}
            self.assertTrue(timing.counter_m5_allowed(s,side,regime,{},vel,proof))
            for bad in ({**proof,'rvol':1.19},{**proof,'micro_shift':False},{**proof,'body_atr':.64},{**proof,'confirmation_timeframe':'3m'}):
                self.assertFalse(timing.counter_m5_allowed(s,side,regime,{},vel,bad))
            for sizing,velocity in (({'late_entry':True},vel),({'exhausted_wave':True},vel),({},dict(vel,blocked=True))):
                self.assertFalse(timing.counter_m5_allowed(s,side,regime,sizing,velocity,proof))
            self.assertFalse(timing.counter_m5_allowed(s,side,{**regime,'side_1h':0},{},vel,proof))

class StorageTests(TestCase):
    def test_history_cache_hit_corruption_and_range(self):
        with TemporaryDirectory() as d:
            fetch=Mock(return_value=[[1,2,3]])
            for _ in range(2): self.assertEqual(storage.cached_rows(d,'BZ','1m',0,60,fetch),[[1,2,3]])
            self.assertEqual(fetch.call_count,1)
            next(Path(d).glob('*.gz')).write_bytes(b'not gzip')
            storage.cached_rows(d,'BZ','1m',0,60,fetch)
            storage.cached_rows(d,'BZ','1m',0,120,fetch)
            self.assertEqual(fetch.call_count,3)

    def test_checkpoint_mismatch_and_no_replay_of_processed_scans(self):
        cfg=replace(fixtures.AdaptiveEntryTests.cfg,core=core)
        data={'instrument':calibrate.INSTRUMENT,'start_ts':NOW,'end_ts':NOW+5*M,'quotes':[{'now':NOW}],
              'source':'SYNTHETIC_TEST','candles':{'1m':[]}}
        def snapshots(data,after=None):
            for t in (NOW,NOW+M,NOW+2*M):
                if after is None or t>after:
                    s=fixtures.AdaptiveEntryTests().snapshot();yield replace(s,now=t)
        def cycle(state,s,cfg):
            state['last_run_ts']=s.now
            state['last_scan_observed_ts']=s.now
            return {'action':'WATCH','analysis':{'rejections':{'EXAMPLE':1}}}
        with TemporaryDirectory() as d,patch.object(calibrate.research,'snapshots',side_effect=snapshots),patch.object(calibrate.research,'snapshot_count',return_value=3),patch.object(engine,'run_cycle',side_effect=cycle) as process:
            cp=Path(d)/'cp.gz'
            artifact,report,labels=calibrate.run(data,cfg,checkpoint=cp,max_scans=2,progress=0)
            self.assertIsNone(artifact);self.assertEqual(report['status'],'PAUSED');self.assertEqual(process.call_count,2)
            with self.assertRaisesRegex(ValueError,'mismatch'):
                calibrate.run({**data,'source':'OTHER'},cfg,checkpoint=cp,resume=True,progress=0)
            with self.assertRaisesRegex(ValueError,'mismatch'):
                calibrate.run(data,replace(cfg,tick=.02),checkpoint=cp,resume=True,progress=0)
            resumed=calibrate.run(data,cfg,checkpoint=cp,resume=True,progress=0)
            self.assertEqual(process.call_count,3)
            full=calibrate.run(data,cfg,progress=0)
            self.assertEqual(resumed,full)
