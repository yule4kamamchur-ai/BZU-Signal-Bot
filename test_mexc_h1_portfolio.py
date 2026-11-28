import csv,json,tempfile,unittest
from pathlib import Path
import mexc_multi_scanner as sc
from replay_h1_portfolio import replay
from mexc_h1_trend import HOUR,MINUTE

class PortfolioReplay(unittest.TestCase):
    def test_synthetic_seven_assets_uses_live_core_and_no_memory(self):
        # Synthetic fixture is purely functional; identical series are not a profitability test.
        base=1790000000000//HOUR*HOUR;start=base+32*HOUR;end=start+2*HOUR
        with tempfile.TemporaryDirectory() as folder:
            p=Path(folder);ticks={a['symbol']:.01 for a in sc.INSTRUMENTS};(p/'ticks.json').write_text(json.dumps(ticks))
            for a in sc.INSTRUMENTS:
                with (p/(a['symbol']+'.csv')).open('w',newline='') as f:
                    w=csv.writer(f);w.writerow(['ts','open','high','low','close','volume'])
                    for t in range(base,end,MINUTE):
                        price=100 if t<base+31*HOUR else 102
                        w.writerow([t,price,price+.1,price-.1,price,100])
            z=replay(p,start,end,180);s=z['final_snapshot'];self.assertEqual(z['cycles'],40);self.assertEqual(len(z['coverage']),7);self.assertGreaterEqual(s['open_count'],3);self.assertLessEqual(s['reserved_usdt'],3.000001);self.assertLessEqual(s['notional_usdt'],500.000001);self.assertEqual(len(z['weekly']),1);self.assertIsNone(z['weekly']['1']['target_20_met']);self.assertFalse((p/'multi_scanner_memory.json').exists())
if __name__=='__main__':unittest.main()
