"""Research engine must not invent fills or read a trade outcome prematurely."""
import unittest

import strategy_research as r


class ConservativePaperReplay(unittest.TestCase):
    def signal(self):
        return {'side':1,'entry':100.,'stop':99.,'tp':101.8,'risk':1.,'known_ts':900_000}

    def bar(self, ts, low, high, close):
        return [ts,100.,high,low,close,100.,'0','0','1']

    def test_no_same_bar_or_exact_touch_fill(self):
        s=self.signal()
        bars=[self.bar(900_000,98.,102.,101.),self.bar(1_080_000,100.,100.3,100.1)]
        self.assertNotEqual(r.execute(s,bars,2_000_000)['status'],'CLOSED')

    def test_fill_bar_stop_wins_even_if_target_possible(self):
        bars=[self.bar(1_080_000,98.,103.,102.)]
        result=r.execute(self.signal(),bars,2_000_000)
        self.assertEqual(result['status'],'CLOSED')
        self.assertEqual(result['result'],'FILL_BAR_STOP')
        self.assertLess(result['net_r'],-1)

    def test_no_fill_after_expiry(self):
        bar=self.bar(self.signal()['known_ts']+61*60_000,98.,102.,101.)
        self.assertEqual(r.execute(self.signal(),[bar],bar[0]+r.THREE)['status'],'UNFILLED')

    def test_end_of_fold_discards_unknown_trade_exit(self):
        bars=[self.bar(1_080_000,99.6,100.1,99.95)]
        self.assertEqual(r.execute(self.signal(),bars,1_500_000)['status'],'CENSORED')

    def test_no_candidate_without_historical_context(self):
        for name in ('SWEEP_RECLAIM_REACTION','SWEEP_THEN_MSS_EVENT',
                     'HTF_TREND_FVG_RETEST','ACCEPTED_BREAK_RETEST','RANGE_EDGE_REJECTION'):
            with self.subTest(name=name):
                self.assertIsNone(r.candidate(name,[],[],[]))


if __name__=='__main__':
    unittest.main()
