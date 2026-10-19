"""Direction symmetry, actual detector witnesses and conditional limit activation.

All candle fixtures are synthetic, explicitly separate from market evidence.
"""
import copy
import unittest
from dataclasses import replace
from functools import wraps
from unittest.mock import patch

import bot_oneshot as core
import full_limit_engine as engine
import setup_paths
import setup_scanner as scanner
import signal_quality as quality
from test_full_limit_engine import NOW,candle,snapshot,order,analysis,full_fixture,lifecycle_snapshot


def witness(setup,side='LONG'):
    _,ctx,_,_=full_fixture(side)
    ctx=copy.deepcopy(ctx)
    rows=[candle(NOW-(60-i)*900000,100,100.4,99.6,100.1) for i in range(60)]
    micro=[candle(NOW-(60-i)*180000,100,100.2,99.8,100.05) for i in range(60)]
    ctx.update(price=100.2 if side=='LONG' else 99.8,atr15=1.,atr3=.5,regime='TRANSITION',regime_bias=side,
        candles={'15m':rows,'3m':micro},structure15={'direction':side,'last_swing_low':100.,'swing_lows':[100.],'swing_highs':[100.]},
        structure3m={'direction':side},structure_shift_15m={'shift':True,'side':side,'level':100.,'age_bars':0,'kind':'CHoCH'},
        htf_fact={'state':'ALIGNED','direction_1h':side,'direction_4h':side,'score':100,'supports':True},
        cvd={'available':True,'rising':False},smt={'supports_long':True,'supports_short':True},
        zones={'consolidation':{'found':True,'low':100.,'high':102.4,'mid':101.2,'compression':2.0,'bars':12},'order_blocks':[],'fvg':[]},
        session={'opening_range_complete':True,'opening_range_low':100.,'opening_range_high':102.,
            'prev_day_low':100.,'prev_day_high':102.,'week_low':99.,'week_high':103.,
            'day_open':100.,'week_open':100.,'asia_complete':True,'asia_low':100.,'asia_high':101.},
        session_name='LONDON',vwap=100.,regime_profile={'efficiency_15m':.8})
    if setup in ('SWEEP_RECLAIM','LIQUIDITY_SWEEP_REVERSAL_SHORT','FAILED_BREAKOUT_SHORT','FAILED_AUCTION_REJECTION'):
        if side=='LONG':rows[-1]=candle(rows[-1].ts,100.1,100.5,99.2,100.3)
        else:rows[-1]=candle(rows[-1].ts,100.3,101.1,99.9,100.0)
        if setup=='FAILED_AUCTION_REJECTION':
            ctx['session'].update(prev_day_high=100.4,prev_day_low=99.8)
        ctx['sweep']=core.detect_sweep(rows)
        ctx['liquidity']={'equal_highs':[{'level':100.4,'touches':3}],'equal_lows':[{'level':99.6,'touches':3}]}
    elif setup=='CAPITULATION_RECOVERY':
        for i in (-3,-2):micro[i]=replace(candle(micro[i].ts,100,100.5,99.,99.9),volume=1000)
    elif setup=='RANGE_EDGE_REVERSAL':
        ctx['price']=100.2 if side=='LONG' else 102.2
    elif setup=='BUYER_EXHAUSTION_SHORT':
        ctx['structure15']['direction']='LONG';ctx['price']=100.3
        for i in range(-12,-4):rows[i]=candle(rows[i].ts,99.6,100.9,99.5,100.6)
        for i in range(-4,0):rows[i]=candle(rows[i].ts,100.2,100.8,100.1,100.3)
    elif setup=='PULLBACK_CONTINUATION':
        ctx['price']=100.2 if side=='LONG' else 99.8
    elif setup=='FRESH_BASE_CONTINUATION':
        rows[-1]=candle(rows[-1].ts,100.,101.2,99.9,101.) if side=='LONG' else candle(rows[-1].ts,102.2,102.3,101.,101.2)
        ctx['price']=100.2 if side=='LONG' else 102.2
    elif setup=='MOMENTUM_NO_PULLBACK_CONTINUATION':
        for i in range(-6,0):
            n=i+6;v=98+n*.55 if side=='LONG' else 102-n*.55
            rows[i]=candle(rows[i].ts,v,v+1.6,v-.1,v+1.4) if side=='LONG' else candle(rows[i].ts,v,v+.1,v-1.6,v-1.4)
        ctx['price']=rows[-1].close
    elif setup=='ACCELERATION_PULLBACK_REENTRY':
        for i in (-12,-8):micro[i]=candle(micro[i].ts,99.6,100.5,99.5,100.4) if side=='LONG' else candle(micro[i].ts,100.4,100.5,99.5,99.6)
        for i in range(-7,0):micro[i]=candle(micro[i].ts,100.1,100.2,99.8,100.)
    elif setup=='TREND_IGNITION':
        for i in range(-13,-1):rows[i]=candle(rows[i].ts,100.,100.2,99.8,100.05)
        rows[-1]=candle(rows[-1].ts,100.,101.5,99.9,101.4) if side=='LONG' else candle(rows[-1].ts,100.,100.1,98.5,98.6)
        ctx['price']=rows[-1].close
    elif setup=='RANGE_COMPRESSION_BREAKOUT':
        ctx['zones']['consolidation'].update(low=99.6,high=100.6,mid=100.1,compression=1.)
        ctx['price']=100.2 if side=='LONG' else 100.
    elif setup=='OPENING_RANGE_BREAKOUT':
        ctx['price']=102.3 if side=='LONG' else 99.7
        micro[-2]=candle(micro[-2].ts,101.,102.,100.9,101.8) if side=='LONG' else candle(micro[-2].ts,100.6,100.7,100.,100.2)
        micro[-1]=candle(micro[-1].ts,101.8,102.4,101.7,102.3) if side=='LONG' else candle(micro[-1].ts,100.2,100.3,99.6,99.7)
    elif setup=='LIQUIDITY_LADDER':
        ctx['liquidity']={'equal_lows':[{'level':100.,'touches':3}], 'equal_highs':[{'level':100.6,'touches':3}]}
    elif setup in ('FAILED_OPENING_RANGE_BREAKOUT','OR_FAILURE_2_SHORT'):
        ctx['session'].update(opening_range_low=99.7,opening_range_high=100.5)
        for i in (-4,-2):rows[i]=candle(rows[i].ts,100.1,100.9,99.9,100.2) if side=='SHORT' else candle(rows[i].ts,100.1,100.3,99.3,100.0)
    elif setup=='MSS_REVERSAL_SHORT':
        for i in range(-20,-14):rows[i]=candle(rows[i].ts,101.,101.2,100.8,101.1)
        rows[-1]=candle(rows[-1].ts,100.5,100.6,99.4,99.5);ctx['price']=99.5
    elif setup=='SESSION_MEAN_RECLAIM':
        rows[-3]=candle(rows[-3].ts,100.7,101.4,100.6,101.3) if side=='LONG' else candle(rows[-3].ts,99.3,99.4,98.6,98.7)
        ctx['price']=100.2 if side=='LONG' else 99.8
    elif setup=='DAILY_WEEKLY_OPEN_RECLAIM':
        rows[-3]=candle(rows[-3].ts,99.5,99.7,99.3,99.5) if side=='LONG' else candle(rows[-3].ts,100.5,100.7,100.3,100.5)
        ctx['price']=100.2 if side=='LONG' else 99.8
    elif setup=='TIME_OF_DAY_ADAPTIVE':ctx['price']=100.2 if side=='LONG' else 100.8
    elif setup=='BREAKOUT_RETEST':
        ctx['session'].update(prev_day_high=100.,week_high=0.,prev_day_low=100.,week_low=0.)
        rows[-1]=candle(rows[-1].ts,100.1,100.4,100.,100.3) if side=='LONG' else candle(rows[-1].ts,99.9,100.,99.6,99.7)
        ctx['price']=rows[-1].close
    return ctx


def scenario(setup,side,tf):
 ctx=witness(setup,side)
 if ctx['price']==100.2 and side=='SHORT':ctx['price']=99.8
 cfg=replace(engine.Config(include_commission=True),core=core,tick=.01)
 bars={t:[candle(NOW-(80-i)*step,100,100.4,99.6,100.1) for i in range(80)] for t,step in engine.TF.items()}
 for t,rows in bars.items():
  rows[-32]=candle(rows[-32].ts,100,106,99.8,100.1)
  rows[-22]=candle(rows[-22].ts,100,100.2,94,100.1)
 native=[replace(c,ts=NOW-(len(ctx['candles']['15m'])-i)*engine.TF[tf]) for i,c in enumerate(ctx['candles']['15m'])]
 micro=scanner.MICRO[tf]
 small=[replace(c,ts=NOW-(len(ctx['candles']['3m'])-i)*engine.TF[micro]) for i,c in enumerate(ctx['candles']['3m'])]
 # Supply real unswept structural destinations, outside the tested pattern.
 native[-32]=candle(native[-32].ts,100,106,99.8,100.1)
 native[-22]=candle(native[-22].ts,100,100.2,94,100.1)
 bars[tf]=native;bars[micro]=small
 ctx['candles']={**bars,'15m':native,'3m':small}
 ctx['analysis_timeframe']=tf
 snap=engine.Snapshot(NOW,ctx['price'],bars,ctx['price']-.001,ctx['price']+.001,True,NOW,cfg.instrument)
 detector=core.DETECTORS[scanner.SETUP_NAMES.index(setup)]
 with engine.market_clock(core,NOW):anchors=setup_paths.candidates(detector,setup,ctx,core)
 anchors=[a for a in anchors if a.side==side]
 if not anchors:return None,'NO_PATTERN',ctx,snap,cfg
 a=anchors[0];a.evidence['setup_timeframe']=tf;a.evidence['micro_timeframe']=micro
 if setup in setup_paths.BREAK_RETEST:
  if setup=='BUYER_EXHAUSTION_SHORT':
   snap.now+=engine.TF[micro];snap.ticker_ts=snap.now;snap.price=a.level-.2
   snap.bid=snap.price-.001;snap.ask=snap.price+.001
   snap.candles[micro]=small+[candle(NOW,a.level+.1,a.level+.2,a.level-.3,a.level-.2)]
  setup_paths.activate(a,ctx,snap,cfg,initial=True)
 diag={};p,why=engine.full_plan(a,ctx,snap,cfg,{},diag)
 return p,why,ctx,snap,cfg

class DetectorReachability(unittest.TestCase):
    def test_all_24_actual_detectors_have_positive_pattern_witnesses(self):
        found={}
        with engine.market_clock(core,NOW):
            for name,detector in zip(scanner.SETUP_NAMES,core.DETECTORS):
                sides=[setup_paths.SINGLE_SIDE[name]] if name in setup_paths.SINGLE_SIDE else ['LONG','SHORT']
                for side in sides:
                    with self.subTest(setup=name,side=side):
                        ctx=witness(name,side)
                        anchors=setup_paths.candidates(detector,name,ctx,core)
                        matches=[a for a in anchors if a.side==side]
                        self.assertTrue(matches,(name,side))
                        found[(name,side)]=len(matches)
        self.assertEqual({k[0] for k in found},set(scanner.SETUP_NAMES))

    def test_both_liquidity_sides_survive_first_long_return(self):
        ctx=witness('LIQUIDITY_LADDER')
        with engine.market_clock(core,NOW):
            self.assertEqual(core.detect_liquidity_ladder(ctx).side,'LONG')
            anchors=setup_paths.candidates(core.detect_liquidity_ladder,'LIQUIDITY_LADDER',ctx,core)
        self.assertEqual({a.side for a in anchors},{'LONG','SHORT'})

    def test_reclaim_checks_day_and_week_instead_of_first_reference_only(self):
        ctx=witness('DAILY_WEEKLY_OPEN_RECLAIM','SHORT')
        ctx['session'].update(day_open=99.8,week_open=100.4)
        # Prior closes on both sides make both independent branches real.
        ctx['candles']['15m'][-3]=candle(NOW-2700000,99,101.2,98.9,101.0)
        ctx['candles']['15m'][-4]=candle(NOW-3600000,99,99.2,98.8,99.)
        ctx['price']=100.1
        with engine.market_clock(core,NOW):
            anchors=setup_paths.candidates(core.detect_daily_weekly_open_reclaim,'DAILY_WEEKLY_OPEN_RECLAIM',ctx,core)
        self.assertEqual({a.side for a in anchors},{'LONG','SHORT'})

    def test_mirrored_range_edges_receive_equal_pattern_scores(self):
        with engine.market_clock(core,NOW):
            long=setup_paths.candidates(core.detect_range_edge_reversal,'RANGE_EDGE_REVERSAL',witness('RANGE_EDGE_REVERSAL','LONG'),core)[0]
            short=setup_paths.candidates(core.detect_range_edge_reversal,'RANGE_EDGE_REVERSAL',witness('RANGE_EDGE_REVERSAL','SHORT'),core)[0]
        self.assertEqual(long.score,short.score)


class DirectionAdmission(unittest.TestCase):
    def test_existing_long_does_not_block_queuing_or_admitting_a_valid_short(self):
        cfg=engine.Config();state=engine.new_state(cfg)
        p=order();p.update(status='OPEN',exit_deadline=NOW+3600000,remaining=1.,last_bar=NOW)
        state['active']=p
        short=order(-1);short.update(id='short',event_key='short',entry=104.,stop=105.,initial_stop=105.,tp=102.,invalidation=105.)
        with patch.object(engine,'analyze',return_value=analysis([short])):
            result=engine.run_cycle(state,snapshot(price=103),cfg)
        self.assertEqual([p['id'] for p in result['selected_plans']],['short'])
        self.assertEqual([p['id'] for p in engine.entry_priority(state,NOW,103,cfg)],['short'])
        snap=lifecycle_snapshot([candle(NOW,103,104.1,102.9,103.5)])
        engine.advance_lifecycle(state,snap,cfg)
        self.assertEqual({p['side'] for p in engine.active_plans(state)},{1,-1})
        self.assertEqual(engine.occupied_slots(state),2)

    def test_shared_countertrend_gate_accepts_confirmed_control_without_new_double_shift(self):
        from test_signal_quality import confirmed_features
        f=confirmed_features();f.update(setup='MSS_REVERSAL_SHORT',side=-1,side15=-1,side3=-1,
            htf_opposition=2,entry_policy='PRETOUCH_LIMIT',detector_confirmed=True,shift15=False,shift3=False)
        self.assertTrue(quality.pretouch_countertrend_allowed(f['setup'],-1,-1,-1))
        self.assertEqual(quality.structural_reason(f),'CONFIRMED')
        f['side3']=1
        self.assertEqual(quality.structural_reason(f),'HTF_REVERSAL_NOT_CONFIRMED')

    def test_waiting_message_contains_only_number_direction_and_entry(self):
        cfg=engine.Config();state=engine.new_state(cfg)
        state['pending']=order();state['second_slot']['pending']=order(-1)
        state['second_slot']['pending']['id']='short';engine.bind_order_numbers(state)
        text=engine.build_message(state,snapshot(),{'action':'WAIT_LIMIT'},cfg)
        section=text.split('ОЧІКУЄМО ВХОДУ — 2\n')[1].split('\n\nЗАКРИТО')[0]
        self.assertEqual(section,'#001 LONG • Вхід: 100\n#002 SHORT • Вхід: 100')
        for term in ('До входу','SL:','TP1','Діє до','виконання перевіряємо','Рішення про вихід'):
            self.assertNotIn(term,section)


class WholeSetupPaths(unittest.TestCase):
    def test_123_direction_timeframe_witnesses_reach_real_planner_quality_and_fill(self):
        count=0
        for setup in scanner.SETUP_NAMES:
            sides=[setup_paths.SINGLE_SIDE[setup]] if setup in setup_paths.SINGLE_SIDE else ['LONG','SHORT']
            for side in sides:
                for tf in scanner.PRIMARY:
                    with self.subTest(setup=setup,side=side,timeframe=tf):
                        p,why,ctx,snap,cfg=scenario(setup,side,tf)
                        self.assertIsNotNone(p,why)
                        decision=quality.assess(p,snap,ctx,cfg,engine,[])
                        self.assertTrue(decision['accepted'],decision['reason'])
                        state=engine.new_state(cfg);state['pending']=p
                        sign=p['side'];entry=p['entry'];risk=p['risk']
                        bar=candle(p['effective_ts'],entry+sign*.2*risk,
                            entry+.3*risk if sign==1 else entry+.02,
                            entry-.02 if sign==1 else entry-.3*risk,entry+sign*.1*risk)
                        engine.advance_lifecycle(state,lifecycle_snapshot([bar]),cfg)
                        self.assertEqual(len(engine.active_plans(state)),1)
                        self.assertEqual(engine.active_plans(state)[0]['entry'],entry)
                        self.assertEqual(engine.active_plans(state)[0]['remaining'],1.)
                        self.assertFalse(state['trades'])
                        count+=1
        self.assertEqual(count,123)

    def test_breakout_waits_then_arms_a_future_limit_after_actual_closed_break(self):
        cfg=replace(engine.Config(),core=core,tick=.01)
        ctx=witness('OPENING_RANGE_BREAKOUT','LONG');ctx['price']=101.
        snap=snapshot(price=101.)
        snap.candles['3m']=[candle(NOW-360000,101,101.2,100.8,101.),candle(NOW-180000,101,101.8,100.9,101.5)]
        with engine.market_clock(core,NOW):
            a=next(a for a in setup_paths.candidates(core.detect_opening_range_breakout,'OPENING_RANGE_BREAKOUT',ctx,core) if a.side=='LONG')
        a.evidence['setup_timeframe']='15m'
        self.assertFalse(setup_paths.activate(a,ctx,snap,cfg,initial=True))
        self.assertEqual(engine.full_plan(a,ctx,snap,cfg,{})[1],'WAIT_CONFIRMED_BREAK_FOR_LIMIT')
        old_expiry=a.expires_ts
        snap.now+=180000;snap.ticker_ts=snap.now;snap.price=102.3;snap.bid=102.299;snap.ask=102.301
        snap.candles['3m'].append(candle(NOW,101.5,102.5,101.4,102.3))
        self.assertTrue(setup_paths.activate(a,ctx,snap,cfg))
        self.assertEqual(a.created_ts,snap.now)
        self.assertEqual(a.evidence['activation_phase'],'LIMIT_RETEST')
        self.assertEqual(a.evidence['break_known_ts'],snap.now)
        self.assertGreater(a.expires_ts,old_expiry)
        fixed=a.created_ts,a.expires_ts
        self.assertFalse(setup_paths.activate(a,ctx,snap,cfg))
        self.assertEqual((a.created_ts,a.expires_ts),fixed)

    def test_buyer_exhaustion_does_not_sell_a_breakout_as_a_marketable_limit(self):
        ctx=witness('BUYER_EXHAUSTION_SHORT','SHORT');cfg=replace(engine.Config(),core=core,tick=.01)
        snap=snapshot(price=100.3)
        with engine.market_clock(core,NOW):a=setup_paths.candidates(core.detect_buyer_exhaustion_short,'BUYER_EXHAUSTION_SHORT',ctx,core)[0]
        a.evidence['setup_timeframe']='15m'
        self.assertEqual(engine.full_plan(a,ctx,snap,cfg,{})[1],'WAIT_CONFIRMED_BREAK_FOR_LIMIT')
        snap.candles['3m']=[candle(NOW-180000,100.2,100.4,100.1,100.3),candle(NOW,100.3,100.4,99.7,99.8)]
        snap.now=NOW+180000;snap.price=99.8
        self.assertTrue(setup_paths.activate(a,ctx,snap,cfg))
        self.assertEqual(a.evidence['activation_phase'],'LIMIT_RETEST')
        self.assertGreater(a.level,snap.price)

    def test_no_third_position_with_mixed_directions_and_an_unlimited_waiting_queue(self):
        cfg=engine.Config();state=engine.new_state(cfg)
        for number,view in engine.slot_views(state):
            p=order(1 if number==1 else -1);p.update(id=str(number),status='OPEN',remaining=1.,exit_deadline=NOW+3600000,last_bar=NOW)
            view['active']=p;engine.store_slot(state,number,view)
        engine.park_pending(state,[order(),{**order(-1),'id':'waiting-short'}])
        self.assertEqual(engine.occupied_slots(state),2)
        self.assertEqual(engine.entry_priority(state,NOW,101.,cfg),[])
        self.assertEqual(len(engine.pending_plans(state)),2)


class CurrentNativeContexts(unittest.TestCase):
    def test_fully_derived_native_contexts_can_open_shorts_without_mocking_detectors_or_targets(self):
        cfg=replace(engine.Config(),core=core)
        for target in ('15m','1H','4H'):
            with self.subTest(timeframe=target):
                snap=snapshot(price=101.25);snap.bid=snap.price-.001;snap.ask=snap.price+.001
                for tf in engine.TF:
                    high,low=(102,100) if tf==target else (102,101.9)
                    snap.candles[tf]=[candle(c.ts,(high+low)/2,high,low,high-.05) for c in snap.candles[tf]]
                if target=='4H':
                    snap.candles['15m']=[candle(c.ts,102,104,100,102) for c in snap.candles['15m']]
                snap.price=200-snap.price;snap.bid=snap.price-.001;snap.ask=snap.price+.001
                snap.candles={tf:[candle(c.ts,200-c.open,200-c.low,200-c.high,200-c.close) for c in rows] for tf,rows in snap.candles.items()}
                state=engine.new_state(cfg)
                result=engine.run_cycle(state,snap,cfg)
                self.assertEqual(result['action'],'PLACE_LIMIT')
                self.assertEqual(result['selected_plans'][0]['side'],-1)
                self.assertTrue(any(p['setup_timeframe']==target for p in result['selected_plans']))

    def test_activation_does_not_reuse_a_break_that_was_already_observed_before_upgrade(self):
        cfg=replace(engine.Config(),core=core)
        ctx=witness('OPENING_RANGE_BREAKOUT','LONG');snap=snapshot(price=102.3)
        with engine.market_clock(core,NOW-900000):
            a=setup_paths.candidates(core.detect_opening_range_breakout,'OPENING_RANGE_BREAKOUT',ctx,core)[0]
        a.evidence['setup_timeframe']='15m'
        snap.candles['3m']=[candle(NOW-540000,101.5,101.8,101.4,101.7),
                            candle(NOW-360000,101.7,102.4,101.6,102.3),
                            candle(NOW-180000,102.3,102.4,102.2,102.3)]
        self.assertFalse(setup_paths.activate(a,ctx,snap,cfg,observed_after=NOW-180000))
        self.assertEqual(a.evidence['activation_phase'],'WAIT_BREAK')

    def test_unsent_waiting_card_is_compacted_without_touching_open_trade_details(self):
        import notification_report
        text="""У СУПРОВОДІ — 1
#028 LONG • 4h
SL: 104.81 • TP1: 108.14
ОЧІКУЄМО ВХОДУ — 1
#033 SHORT • 1h • Ретест
Вхід: 105.73 • До входу: 0.619%
SL: 104.62 • TP1: 107.43 • TP2: 109.06
Рішення про вихід — після TP1
Діє до 20.10 10:21
Ціна на рівні або за ним; виконання перевіряємо за закритими свічками.
ЗАКРИТО З МИНУЛОГО ЗВІТУ
#029 ЗАКРИТО: -1.02R
стоп"""
        cleaned=notification_report.clean_queued_message(text)
        self.assertIn('SL: 104.81 • TP1: 108.14',cleaned)
        self.assertIn('#033 SHORT • Вхід: 105.73',cleaned)
        self.assertIn('#029 ЗАКРИТО: -1.02R',cleaned)
        self.assertNotIn('До входу:',cleaned)
        self.assertNotIn('TP2:',cleaned)
        self.assertNotIn('Діє до',cleaned)


if __name__=='__main__':unittest.main()
