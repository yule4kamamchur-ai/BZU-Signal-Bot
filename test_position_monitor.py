"""Single-position policy and as-of structural review: no trading edge implied."""
import copy
import unittest
from unittest.mock import patch

import position_monitor as monitor
import notification_report as notification
import full_limit_engine as engine
from test_full_limit_engine import NOW, analysis, order, snapshot

STEP=monitor.STEPS


def bar(ts, close, high=None, low=None, open=None, confirmed=True):
    return engine.Candle(ts, close if open is None else open,
                         high if high is not None else close+.25,
                         low if low is not None else close-.25, close, 1., confirmed)


def market(against=False):
    current=NOW+14*STEP['1H']
    rows={}
    for tf,n in [('3m',70),('15m',70),('1H',70),('4H',20)]:
        step=STEP[tf]
        bars=[bar(current-(n-i)*step,100+0.03*i) for i in range(n)]
        rows[tf]=bars
    if against:
        # Known support at ~101.6; a newly closed candle breaks it.
        rows['15m'][-1]=bar(rows['15m'][-1].ts,98.,open=100.,high=100.1,low=97.7)
        rows['1H'][-1]=bar(rows['1H'][-1].ts,98.,open=100.,high=100.1,low=97.7)
    return engine.Snapshot(now=current,price=100.,candles=rows,trusted=True,ticker_ts=current)


class MonitorTests(unittest.TestCase):
    def test_closed_structural_break_warns_without_modifying_position(self):
        p={'id':'opened','side':1,'entry':100,'stop':99,'tp':103}
        before=copy.deepcopy(p)
        verdict=monitor.review(p,market(True))
        self.assertEqual(verdict['status'],'REVERSAL_RISK')
        self.assertTrue(verdict['break_15m'])
        self.assertEqual(p,before)

    def test_future_or_unconfirmed_break_does_not_change_assessment(self):
        s=market();before=monitor.review({'side':1},s)
        extra=bar(s.now+STEP['15m'],10.,high=110.,low=9.,confirmed=True)
        s.candles['15m'].extend([extra,bar(s.now-STEP['15m'],10.,high=110.,low=9.,confirmed=False)])
        self.assertEqual(before,monitor.review({'side':1},s))

    def test_missing_bars_are_unknown_not_safe_forecast(self):
        s=market();s.candles['1H']=[]
        self.assertEqual(monitor.review({'side':-1},s)['status'],'INSUFFICIENT')

    def test_active_book_blocks_independent_long_and_short(self):
        cfg=engine.Config();state=engine.new_state(cfg)
        active=order(1);active.update(id='open-long',status='OPEN',remaining=1.,
                   filled_ts=NOW-180000,last_bar=NOW,exit_deadline=NOW+3600000)
        state['active']=active
        for side in (1,-1):
            proposed=order(side);proposed.update(id='candidate-'+str(side),event_key='event-'+str(side),
                                                 entry=97. if side==1 else 105.)
            with patch.object(engine,'analyze',return_value=analysis([proposed])):
                outcome=engine.run_cycle(copy.deepcopy(state),snapshot(),cfg)
            self.assertFalse(outcome['selected_plans'])
            self.assertEqual(outcome['analysis']['direction_policy']['reason'],'ACTIVE_POSITION_MONITOR_ONLY')

    def test_report_indicates_monitor_waiting_and_close_without_old_noise(self):
        cfg=engine.Config();state=engine.new_state(cfg)
        p=order(1);p.update(id='open-long',order_number=5,status='OPEN',remaining=1.,
                             exit_deadline=NOW+3600000)
        state['active']=p
        result={'action':'FOLLOW','analysis':{'position_reviews':[{'order_id':'open-long',
             'status':'REVERSAL_RISK','reason':'1H злам структури проти позиції'}]},'events':[]}
        text=engine.build_message(state,snapshot(),result,cfg)
        self.assertIn('🔵 СУПРОВІД — 1',text)
        self.assertIn('🔴 Структура: ризик розвороту',text)
        self.assertNotIn('Обраний сценарій:',text)
        self.assertNotIn('ЗАКРИТО З МИНУЛОГО ЗВІТУ',text)
        self.assertNotIn('Нових закриттів немає',text)
        queued=notification.clean_queued_message('Обраний сценарій: ОЧІКУВАННЯ • NO_VALID_ICT_SCENARIO\n'
            'ЗАКРИТО З МИНУЛОГО ЗВІТУ\nНових закриттів немає.\n✅ #001 ЗАКРИТО: +1.00R')
        self.assertIn('ЗАКРИТО: +1.00R',queued)
        self.assertNotIn('NO_VALID_ICT_SCENARIO',queued)
        self.assertNotIn('Нових закриттів немає',queued)

if __name__ == '__main__': unittest.main()
