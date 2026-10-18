"""Bounded public-market evidence for reproducible later replay. No credentials."""
from __future__ import annotations

import copy

SCHEMA='closed_market_evidence_v1'
RETENTION_MS=30*86_400_000


def known_test_contamination(archive):
    """Identify exact old LiveTickTests quotes; do not infer from price alone."""
    fixtures = {(1791288000000, 70., 69.995, 70.005),
                (1791288900000, 70.02, 70.01, 70.03)}
    matches = [q for q in (archive or {}).get('quotes', [])
               if (q.get('now'), q.get('price'), q.get('bid'), q.get('ask')) in fixtures]
    return {'known_test_quotes': len(matches),
            'known_test_quote_timestamps': [q['now'] for q in matches],
            'safe_research_after_ts': max((q['now'] for q in matches), default=None),
            'status': 'KNOWN_TEST_FIXTURES_PRESENT' if matches else 'NO_KNOWN_TEST_QUOTES',
            'meaning': 'A match identifies specific test data; absence is not a full provenance certification'}


def record(path,snapshot,cfg,engine):
    data=engine.read_json(path)
    if data and (data.get('schema')!=SCHEMA or data.get('instrument')!=cfg.instrument):
        raise ValueError('Unknown market archive; preserve the file')
    if not data:
        data={'schema':SCHEMA,'instrument':cfg.instrument,'candles':{},'quotes':[]}
    if data.get('quotes') and snapshot.now<=max(q['now'] for q in data['quotes']):
        return {'status':'UNCHANGED','quotes':len(data['quotes']),
                'closed_bars':{tf:len(rows) for tf,rows in data['candles'].items()}}
    data=copy.deepcopy(data)
    lower=snapshot.now-RETENTION_MS
    for tf,step in engine.TF.items():
        rows={}
        for raw in data.get('candles',{}).get(tf,[]):
            c=engine.Candle.parse(raw)
            if lower<=c.ts and c.ts+step<=snapshot.now and c.confirmed:
                rows[c.ts]=raw
        for c in snapshot.candles.get(tf,[]):
            if lower<=c.ts and c.ts+step<=snapshot.now and c.confirmed:
                # Preserve what was known on first observation. Later provider
                # corrections cannot silently rewrite prior market evidence.
                rows.setdefault(c.ts,[c.ts,c.open,c.high,c.low,c.close,c.volume,'0','0','1'])
        data['candles'][tf]=[rows[ts] for ts in sorted(rows)]
    quotes={q['now']:q for q in data.get('quotes',[]) if lower<=q['now']<=snapshot.now}
    quotes.setdefault(snapshot.now,{'now':snapshot.now,'ticker_ts':snapshot.ticker_ts,
        'price':snapshot.price,'bid':snapshot.bid,'ask':snapshot.ask,'trusted':snapshot.trusted,
        'tick':cfg.tick,'version':engine.VERSION})
    data['quotes']=[quotes[ts] for ts in sorted(quotes)]
    data['updated_at']=engine.iso(snapshot.now)
    engine.atomic_write(path,data)
    return {'status':'RECORDED','quotes':len(data['quotes']),
            'closed_bars':{tf:len(rows) for tf,rows in data['candles'].items()}}
