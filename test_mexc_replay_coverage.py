import contextlib,csv,io,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import mexc_multi_scanner as sc
from replay_h1_portfolio import replay,select_window,CoverageError,DAY,iso_ms
from mexc_replay_v154 import download
from mexc_h1_trend import HOUR,MINUTE

BASE=iso_ms('2026-11-01')

def bars(first,end,skip=()):
    return [sc.Bar(t,100,101,99,100,1) for t in range(first,end,MINUTE) if t not in skip]

def series(first,end,skip=()):
    raw=bars(first,end,skip);hrs=sc.aggregate_bars(raw,60,3600,end);m15=sc.aggregate_bars(raw,60,900,end)
    return raw,[b.ts for b in raw],hrs,[b.ts for b in hrs],m15,[b.ts for b in m15]

class ReplayCoverage(unittest.TestCase):
    def test_old_start_rejected_strict(self):
        end=BASE+12*DAY; s={'UKOIL_USDT':series(BASE,end)}
        report,_,_=select_window(s,BASE-150*DAY,end,'MIXED_PEAK_RANGE','strict')
        self.assertTrue(report['errors']);self.assertEqual(report['assets']['UKOIL_USDT']['h1_before_requested_start'],0)

    def test_common_interval_clips_both_edges_and_retains_gap(self):
        end=BASE+13*DAY
        s={'UKOIL_USDT':series(BASE,end),'LINK_USDT':series(BASE+DAY,end-HOUR,skip=(BASE+5*DAY,))}
        report,a,b=select_window(s,BASE-150*DAY,end,'MIXED_PEAK_RANGE','available')
        self.assertFalse(report['errors']);self.assertEqual(a,BASE+3*DAY);self.assertEqual(b,BASE+12*DAY)
        self.assertTrue(report['clipped']);self.assertEqual(report['duration_days'],9)
        self.assertEqual(report['assets']['LINK_USDT']['test_m1_observed'],9*1440-1)
        self.assertEqual(report['assets']['LINK_USDT']['test_gap_count'],1)
        self.assertGreaterEqual(report['assets']['LINK_USDT']['h1_before_effective_start'],31)

    def test_too_short_or_insufficient_hours(self):
        s={'UKOIL_USDT':series(BASE,BASE+2*DAY)}
        report,_,_=select_window(s,BASE-150*DAY,BASE+2*DAY,'PEAK_CURRENT','available')
        self.assertTrue(report['errors'])
        s={'UKOIL_USDT':series(BASE,BASE+20*HOUR)}
        report,_,_=select_window(s,BASE-150*DAY,BASE+20*HOUR,'PEAK_CURRENT','available')
        self.assertTrue(report['errors'])

    def test_range_only_does_not_require_31h(self):
        end=BASE+12*HOUR
        s={'UKOIL_USDT':series(BASE,end)}
        report,_,_=select_window(s,BASE+6*HOUR,end,'RANGE_ONLY','strict')
        self.assertFalse(report['errors'])

    def test_replay_saves_coverage_on_failure_and_success(self):
        end=BASE+10*DAY;sym='UKOIL_USDT'
        with tempfile.TemporaryDirectory() as folder,contextlib.redirect_stdout(io.StringIO()):
            p=Path(folder);(p/'ticks.json').write_text(json.dumps({sym:.01}));out=p/'coverage.json'
            with (p/(sym+'.csv')).open('w',newline='') as f:
                w=csv.writer(f);w.writerow(['ts','open','high','low','close','volume'])
                w.writerows((t,100,101,99,100,1) for t in range(BASE,end,MINUTE))
            with self.assertRaises(CoverageError): replay(p,BASE-150*DAY,end,3600,symbols=[sym],coverage_out=out)
            self.assertEqual(json.loads(out.read_text())['status'],'INSUFFICIENT_COVERAGE')
            result=replay(p,BASE-150*DAY,end,3600,symbols=[sym],coverage_mode='available',coverage_out=out,profile='MIXED_PEAK_RANGE')
            self.assertEqual(result['coverage_window']['status'],'AVAILABLE_INTERVAL');self.assertEqual(result['cycles'],8*24)
            self.assertEqual(result['weekly']['1']['days'],7);self.assertEqual(result['weekly']['2']['days'],1)
            self.assertFalse((p/'multi_scanner_memory.json').exists())

    def test_empty_cache_is_retried_and_valid_chunk_reused(self):
        sym='UKOIL_USDT';start=BASE;end=BASE+2*MINUTE
        payload={'success':True,'data':{'time':[start//1000,(start+MINUTE)//1000],'open':[100,100],'high':[101,101],'low':[99,99],'close':[100,100],'vol':[1,1]}}
        with tempfile.TemporaryDirectory() as folder,contextlib.redirect_stdout(io.StringIO()):
            p=Path(folder);chunk=p/'chunks'/sym/f'{start}_{end}.json';chunk.parent.mkdir(parents=True);chunk.write_text('{"success":true,"data":{"time":[]}}')
            with patch.object(sc,'fetch_contract_ticks',return_value={sym:.01}),patch.object(sc,'_http_json',return_value=payload) as request:
                download(sc,p,start,end,symbols=[sym]);self.assertEqual(request.call_count,1)
                download(sc,p,start,end,symbols=[sym]);self.assertEqual(request.call_count,1)
            self.assertEqual(json.loads(chunk.read_text()),payload)
            self.assertEqual(len((p/(sym+'.csv')).read_text().splitlines()),3)

if __name__=='__main__': unittest.main()
