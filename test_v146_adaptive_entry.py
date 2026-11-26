"""v14.4.7 causal timing and independent route evidence (no network/orders)."""
from dataclasses import replace
from unittest import TestCase
from unittest.mock import patch
import adaptive_entry as timing
import bot_oneshot as core
import full_limit_engine as engine
import expectancy_model as ev
import signal_quality as quality
import shadow_ledger as shadow
from test_v145_expectancy import CTX, row

M=60_000
NOW=1794000000000//(15*M)*(15*M)

class AdaptiveEntryTests(TestCase):
    cfg=engine.Config(tick=.01,include_commission=False)

    def snapshot(self, side=1, witness=False, shock=False):
        def candle(ts,o,h,l,c,v=100,confirmed=True):
            if side==-1: o,h,l,c=200-o,200-l,200-h,200-c
            return engine.Candle(ts,o,h,l,c,v,confirmed)
        minutes=[candle(NOW-i*M,100.02,100.04,100.00,100.02) for i in range(100,0,-1)]
        price=100.04
        if witness:
            minutes[-1]=candle(NOW-M,100.00,100.10,99.98,100.08)
            price=100.08
        if shock:
            minutes[-1]=candle(NOW-M,100.26,100.27,99.98,99.99,300)
            price=99.99
        price=price if side==1 else 200-price
        return engine.Snapshot(NOW,price,{'1m':minutes,'15m':[
            candle(NOW-i*15*M,100.1,100.35,99.95,100.1) for i in range(40,0,-1)]},
            price-.005,price+.005,True,NOW,self.cfg.instrument)

    def zone(self, side=1):
        return dict(kind='FVG_PULLBACK',low=99.98,high=100.02,known_ts=NOW-20*M,side=side,timeframe='15m')

    def test_all_24_original_setups_and_version(self):
        self.assertEqual(len(core.DETECTORS),24)
        self.assertEqual(set(core.DETECTED_SETUP_TYPES),set(quality.CONTRACTS))
        self.assertEqual(core.BOT_VERSION,engine.VERSION)
        self.assertIn('14.4.7',engine.VERSION)

    def test_closed_complete_buckets_no_future_or_missing_minute(self):
        s=self.snapshot()
        s.candles['1m'] += [engine.Candle(NOW,100,101,99,100,1,True),engine.Candle(NOW-M,100,101,99,100,1,False)]
        self.assertEqual(timing.rows(s,'1m')[-1].ts,NOW-M)
        for tf,step in timing.STEPS.items():
            self.assertTrue(all(c.ts+step<=NOW and c.confirmed for c in timing.rows(s,tf)))
        g=replace(s,candles={**s.candles,'1m':[c for c in s.candles['1m'] if c.ts!=NOW-2*M]})
        self.assertNotEqual(timing.rows(g,'3m')[-1].ts,NOW-3*M)
        self.assertNotEqual(timing.rows(g,'5m')[-1].ts,NOW-5*M)

    def test_m1_m3_m5_witness_for_both_directions(self):
        for side in (1,-1):
            for tf in timing.STEPS:
                with self.subTest(side=side,tf=tf):
                    s=self.snapshot(side,witness=True)
                    proof=timing.trigger(s,side,self.zone(side),100.,1.,self.cfg,engine,tf)
                    self.assertIsNotNone(proof)
                    self.assertEqual(proof['known_ts'],NOW)
                    self.assertEqual(proof['confirmation_timeframe'],tf)
                    self.assertTrue(proof['micro_shift'])
                    self.assertEqual(timing.route({'order_type':'MARKET','market_trigger':proof}),tf.upper().replace('1M','M1').replace('3M','M3').replace('5M','M5'))

    def test_micro_rejects_stale_quote_late_price_and_future_signal(self):
        s=self.snapshot(witness=True)
        for bad in (replace(s,ticker_ts=NOW-4*M),replace(s,ask=100.50,price=100.50),replace(s,trusted=False)):
            self.assertIsNone(timing.trigger(bad,1,self.zone(),100.,1.,self.cfg,engine,'1m'))
        self.assertIsNone(timing.trigger(s,1,{**self.zone(),'known_ts':NOW},100.,1.,self.cfg,engine,'1m'))

    def test_early_limit_requires_calm_fresh_aligned_passive_quote(self):
        for side in (1,-1):
            s=self.snapshot(side); z=self.zone(side)
            regime={'side_1h':side,'side_4h':side}
            v=timing.approach(s,side,z,100.,.01,engine)
            self.assertTrue(v['calm'],v)
            self.assertTrue(timing.early_allowed(s,side,100.,z,regime,{},v,.01,engine))
            self.assertFalse(timing.early_allowed(s,side,100.,z,{**regime,'side_4h':-side},{},v,.01,engine))
            self.assertFalse(timing.early_allowed(s,side,100.,z,regime,{'rejection_required':True},v,.01,engine))
            self.assertFalse(timing.early_allowed(s,side,100.,{**z,'known_ts':NOW-9*15*M},regime,{},v,.01,engine))
            bad=self.snapshot(side,shock=True)
            speed=timing.approach(bad,side,z,100.,.01,engine)
            self.assertTrue(speed['micro_shock'],speed)
            self.assertTrue(speed['blocked'])
            self.assertFalse(timing.early_allowed(bad,side,100.,z,regime,{},speed,.01,engine))

    def test_names_and_ny_alone_no_m15_but_risk_still_requires_it(self):
        s=replace(self.snapshot(),now=NOW//ev.DAY*ev.DAY+15*3600000)
        regime={'side_1h':1,'side_4h':1}
        for setup in quality.ADVERSE_LIMIT_SETUPS:
            q=quality.execution_sizing(s,1,100.,regime,wave_ts=None,rvol=1.4,room_r=2.,setup=setup,source_strategy='FVG_PULLBACK')
            self.assertFalse(q['rejection_required'],setup)
            self.assertTrue(q['adverse_limit_guard'])
        for regime0,late in (({'side_1h':1,'side_4h':-1},False),(regime,True),({**regime,'active_structure':{'wave_atr':4},'trend_strength':.1},False)):
            q=quality.execution_sizing(s,1,100.,regime0,wave_ts=None,rvol=1.4,room_r=2.,late=late)
            self.assertTrue(q['rejection_required'])
        self.assertEqual(timing.route({'order_type':'MARKET','market_trigger':{'pattern':'M15_CONFIRMED_LEVEL_REJECTION'}}),'M15')
        self.assertEqual(timing.route({'order_type':'LIMIT'}),'LEGACY_LIMIT')

    def test_route_votes_dedup_and_unfilled_counted_only_as_attempts(self):
        primary=row(0,1.0,timing_episode_id='zone',timing_route='EARLY_LIMIT')
        alternative=row(0,2.0,ctx=[*CTX[:-1],'M3'],timing_episode_id='zone',timing_route='M3',shadow_timing_only=True)
        unfilled=row(1,None,ctx=[*CTX[:-1],'M3'],close_reason='LIMIT_TTL',timing_episode_id='zone2',shadow_timing_only=True)
        clean=ev.labels([primary,alternative,{**alternative,'id':'dupe'},unfilled],10*ev.DAY)
        self.assertEqual(len(clean),3)
        c=ev.counts(clean)
        self.assertEqual(c['[]'],[1.,1.,1.,0.,1.,0.])
        self.assertEqual(c['["ROUTE","M3"]'],[2.,1.,1.,0.,2.,0.])
        self.assertEqual(c['["ROUTE","EARLY_LIMIT"]'],[1.,1.,1.,0.,1.,0.])
        self.assertFalse(ev.walk_forward(clean,0,10*ev.DAY,route='M3')['validated'])

    def test_unvalidated_route_cannot_change_risk(self):
        s=self.snapshot(); s._expectancy_calibration=({'[]':[60,60,50,10,100,10]},{}); s._validated_timing_routes=('M15',)
        p={'entry':100.,'risk':1.,'side':1,'setup':'BREAKOUT_RETEST','timing_route':'M3','risk_multiplier':.35,'structural_risk_cap':1.,'meta_context':CTX[:1]+[1,'LOWER','LONDON']}
        ev.apply(p,s,self.cfg,engine)
        self.assertEqual(p['expectancy_status'],'MONITOR_ONLY_ROUTE_UNVALIDATED')
        self.assertEqual(p['risk_multiplier'],.35)
        self.assertEqual(ev.ranking_adjustment(p),0.)

    def test_shadow_route_id_distinct_and_no_portfolio_placement(self):
        s=self.snapshot(); state=engine.new_state(self.cfg)
        base=dict(id='zone',event_key='zone',entry_zone=self.zone(),entry=100.,stop=99.,tp=102.,risk=1.,side=1,
                  placed_ts=NOW,expires_ts=NOW+100*M,order_type='LIMIT',policy=engine.bzu_policy.POLICY,
                  exit_policy=engine.bzu_policy.FULL_POSITION_EXIT,meta_context=['BREAKOUT_RETEST',1,'LOWER','LONDON'],
                  timing_route='EARLY_LIMIT',expectancy_context=CTX,expectancy_label_schema=ev.LABEL_SCHEMA)
        alt={**base,'id':'variant','timing_route':'M5','expectancy_context':[*CTX[:-1],'M5'],'shadow_timing_only':True}
        shadow.watch(state,[base,alt,alt],s,self.cfg,engine)
        self.assertEqual(len(shadow.ensure(state)['pending']),2)
        self.assertEqual(base['timing_episode_id'],alt['timing_episode_id'])
        self.assertNotEqual(base['calibration_id'],alt['calibration_id'])
        self.assertFalse(engine.active_plans(state)); self.assertFalse(engine.pending_plans(state)); self.assertFalse(state['events'])

