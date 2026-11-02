"""Out-of-sample research guardrails, separate from executable strategy."""
import unittest
from types import SimpleNamespace

import research_short_continuations as research
import research_zone_policy_1220 as zones
import entry_playbooks as play


def bar(low, high, close=100):
    return SimpleNamespace(low=low,high=high,close=close)


class ResearchChecks(unittest.TestCase):
    def test_1pct_stop_and_2pct_tp1_are_true_minima_short(self):
        signal=play.Setup('MICRO_BOS_FVG',-1,100.,100.3,1,0,.2,'closed')
        metrics=research.label_details(signal,[bar(99.8,100.1),bar(97.,99.9,98.)])
        self.assertGreaterEqual(metrics['risk_pct'],1.)
        self.assertGreaterEqual(metrics['target_pct'],2.)
        self.assertEqual(metrics['outcome'],'TP_PROXY')
        self.assertLess(metrics['net_r'],metrics['gross_r'])

    def test_same_bar_sl_and_tp_conservatively_stop(self):
        signal=play.Setup('MICRO_BOS_FVG',-1,100.,100.3,1,0,.2,'closed')
        future=[bar(99.5,100.1),bar(97.,102.)]
        result=research.label_details(signal,future)
        self.assertEqual(result['outcome'],'STOP')
        self.assertLess(result['net_r'],0.)

    def test_exact_touch_does_not_invent_paper_fill(self):
        signal=play.Setup('MICRO_BOS_FVG',-1,100.,100.3,1,0,.2,'closed')
        self.assertEqual(research.label_details(signal,[bar(99.,100.0)])['outcome'],'UNFILLED')


if __name__ == '__main__':unittest.main()
