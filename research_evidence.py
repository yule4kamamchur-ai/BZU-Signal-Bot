"""Separate, provenance-preserving market evidence. No legacy auto-certification."""
from __future__ import annotations

import hashlib
import json

SCHEMA = 'bzu_controlled_market_v2'
ORIGIN = 'OKX_PUBLIC_RUNTIME_COLLECTOR'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()


def empty(instrument):
    data = {'schema': SCHEMA, 'instrument': instrument, 'source': ORIGIN,
            'candles': {}, 'first_seen': {}, 'quotes': [], 'batches': [], 'revisions': [],
            'smt_asset_id':None,'smt_candles_15m':[],'smt_first_seen':{},'smt_revisions':[],
            'provenance_limit': 'Collector attribution and content integrity; not an exchange signature'}
    return seal(data)


def seal(data):
    data['content_sha256'] = digest({k: v for k, v in data.items() if k != 'content_sha256'})
    return data


def verify(data, engine):
    if data.get('schema') != SCHEMA or data.get('source') != ORIGIN:
        raise ValueError('Unverified research schema/source; keep legacy and synthetic data separate')
    if data.get('content_sha256') != digest({k: v for k, v in data.items() if k != 'content_sha256'}):
        raise ValueError('Research evidence checksum differs')
    batch_map = {b['id']: b for b in data['batches']}
    if len(batch_map) != len(data['batches']):
        raise ValueError('Duplicate provenance batch')
    for batch in data['batches']:
        if batch['id'] != digest({k: v for k, v in batch.items() if k != 'id'}):
            raise ValueError('Provenance batch checksum differs')
        if batch['origin'] != ORIGIN or batch['instrument'] != data['instrument']:
            raise ValueError('Mixed instrument or source in research batch')
    for tf, rows in data['candles'].items():
        if tf not in engine.TF:
            raise ValueError('Unknown research timeframe')
        previous = -1
        for raw in rows:
            candle = engine.Candle.parse(raw)
            stamp = data['first_seen'][tf].get(str(candle.ts))
            if not stamp or stamp['batch_id'] not in batch_map:
                raise ValueError('Missing candle provenance')
            batch = batch_map[stamp['batch_id']]
            if (not candle.confirmed or candle.ts % engine.TF[tf] or candle.ts <= previous
                    or candle.ts+engine.TF[tf] > stamp['observed_ts']
                    or stamp['observed_ts'] != batch['observed_ts']):
                raise ValueError('Noncausal or duplicate research candle')
            previous = candle.ts
    previous = -1
    for quote in data['quotes']:
        batch = batch_map.get(quote['batch_id'])
        if (not batch or quote['now'] != batch['observed_ts'] or quote['now'] <= previous
                or quote.get('trusted') is not True or not 0 <= quote['now']-quote['ticker_ts'] <= 120000
                or quote['price'] <= 0 or quote['tick']<=0 or not 0 < quote['bid'] <= quote['ask']):
            raise ValueError('Invalid or unverified archived quote')
        previous = quote['now']
    for revision in data['revisions']:
        c=engine.Candle.parse(revision['bar'])
        batch=batch_map.get(revision['batch_id'])
        if (not batch or c.ts!=revision['ts'] or not c.confirmed
                or c.ts+engine.TF[revision['timeframe']]>revision['observed_ts']
                or revision['observed_ts']!=batch['observed_ts']
                or revision['revised_sha256']!=digest(revision['bar'])):
            raise ValueError('Invalid revision provenance')
    for raw in data.get('smt_candles_15m',[]):
        c=engine.Candle.parse(raw)
        stamp=data.get('smt_first_seen',{}).get(str(c.ts),{})
        if (not data.get('smt_asset_id') or stamp.get('batch_id') not in batch_map
                or not c.confirmed or c.ts+engine.TF['15m']>stamp.get('observed_ts',0)):
            raise ValueError('Missing/noncausal SMT provenance')
    for revision in data.get('smt_revisions',[]):
        batch=batch_map.get(revision['batch_id']);c=engine.Candle.parse(revision['bar'])
        if (not batch or revision['observed_ts']!=batch['observed_ts']
                or c.ts!=revision['ts'] or c.ts+engine.TF['15m']>revision['observed_ts']
                or revision['revised_sha256']!=digest(revision['bar'])):
            raise ValueError('Invalid SMT revision provenance')
    return {'status': 'VERIFIED_COLLECTOR_ATTRIBUTION', 'quotes': len(data['quotes']),
            'batches': len(data['batches']), 'fingerprint': data['content_sha256'],
            'provider_signature': False}


def record(path, snapshot, cfg, engine):
    provenance = snapshot.metadata.get('research_provenance', {})
    if (provenance.get('origin') != ORIGIN or provenance.get('instrument') != cfg.instrument
            or snapshot.instrument != cfg.instrument
            or provenance.get('observed_ts') != snapshot.now
            or snapshot.metadata.get('price_source') != 'OKX_TICKER'
            or not snapshot.trusted or not 0 <= snapshot.now-snapshot.ticker_ts <= 120000
            or not 0 < snapshot.bid <= snapshot.ask or snapshot.price <= 0):
        return {'status': 'NOT_RECORDED_UNVERIFIED_SOURCE'}
    data = engine.read_json(path) or empty(cfg.instrument)
    verify(data, engine)
    if data['instrument'] != cfg.instrument:
        raise ValueError('Research instrument changed; use another archive')
    if data['quotes'] and snapshot.now <= data['quotes'][-1]['now']:
        return {'status': 'UNCHANGED', 'quotes': len(data['quotes'])}
    payload = {'instrument': cfg.instrument, 'origin': ORIGIN, 'observed_ts': snapshot.now,
               'collector_version': engine.VERSION, 'quote_source': 'OKX_TICKER',
               'price_tick': cfg.tick, 'timeframe_sources': snapshot.metadata.get('htf_source', {}),
               'snapshot_sha256': digest({'quote': [snapshot.price, snapshot.bid, snapshot.ask, snapshot.ticker_ts],
                 'candles': {tf: [vars(c) for c in rows] for tf, rows in snapshot.candles.items()},
                 'smt_asset_id':snapshot.metadata.get('smt_asset_id'),
                 'smt_candles':[vars(c) for c in snapshot.smt_candles]})}
    payload['id'] = digest(payload)
    data['batches'].append(payload)
    for tf, step in engine.TF.items():
        rows = {engine.Candle.parse(raw).ts: raw for raw in data['candles'].get(tf, [])}
        current_versions=dict(rows)
        for revision in data['revisions']:
            if revision['timeframe']==tf:
                current_versions[revision['ts']]=revision['bar']
        seen = data['first_seen'].setdefault(tf, {})
        for c in snapshot.candles.get(tf, []):
            if not c.confirmed or c.ts+step > snapshot.now:
                continue
            raw = [c.ts, c.open, c.high, c.low, c.close, c.volume, '0', '0', '1']
            if c.ts in rows:
                if current_versions[c.ts] != raw:
                    revision = {'timeframe': tf, 'ts': c.ts, 'observed_ts': snapshot.now,
                                'original_sha256': digest(current_versions[c.ts]), 'revised_sha256': digest(raw),
                                'bar':raw,'batch_id':payload['id']}
                    data['revisions'].append(revision)
                    current_versions[c.ts]=raw
                continue
            rows[c.ts] = raw
            seen[str(c.ts)] = {'observed_ts': snapshot.now, 'batch_id': payload['id']}
        data['candles'][tf] = [rows[ts] for ts in sorted(rows)]
    peer=snapshot.metadata.get('smt_asset_id')
    if peer and snapshot.smt_candles:
        if data.get('smt_asset_id') not in (None,peer):
            raise ValueError('SMT source changed; split the research archive')
        data['smt_asset_id']=peer
        peer_rows={engine.Candle.parse(raw).ts:raw for raw in data.get('smt_candles_15m',[])}
        peer_current=dict(peer_rows)
        for revision in data.get('smt_revisions',[]):
            peer_current[revision['ts']]=revision['bar']
        seen=data.setdefault('smt_first_seen',{})
        for c in snapshot.smt_candles:
            if not c.confirmed or c.ts+engine.TF['15m']>snapshot.now:
                continue
            raw=[c.ts,c.open,c.high,c.low,c.close,c.volume,'0','0','1']
            if c.ts not in peer_rows:
                peer_rows[c.ts]=raw
                seen[str(c.ts)]={'observed_ts':snapshot.now,'batch_id':payload['id']}
            elif peer_current[c.ts]!=raw:
                data.setdefault('smt_revisions',[]).append({'ts':c.ts,'bar':raw,'observed_ts':snapshot.now,
                    'batch_id':payload['id'],'revised_sha256':digest(raw)})
        data['smt_candles_15m']=[peer_rows[ts] for ts in sorted(peer_rows)]
    data['quotes'].append({'now': snapshot.now, 'ticker_ts': snapshot.ticker_ts,
                          'price': snapshot.price, 'bid': snapshot.bid, 'ask': snapshot.ask,
                          'trusted': True, 'tick': cfg.tick, 'version': engine.VERSION, 'batch_id': payload['id'],
                          'smt_available':bool(peer and snapshot.smt_candles)})
    seal(data)
    verify(data, engine)
    engine.atomic_write(path, data)
    return {'status': 'RECORDED_VERIFIED_SOURCE', 'quotes': len(data['quotes']),
            'fingerprint': data['content_sha256'], 'revisions': len(data['revisions'])}


def dataset(data, engine):
    verify(data, engine)
    if len(data['quotes']) < 2:
        raise ValueError('Clean archive has insufficient observed scan times')
    ticks = {q['tick'] for q in data['quotes']}
    if len(ticks) != 1:
        raise ValueError('Split research at exchange tick-size changes')
    return {'instrument': data['instrument'], 'tick': ticks.pop(),
            'start_ts': data['quotes'][0]['now'], 'end_ts': data['quotes'][-1]['now'],
            'candles': data['candles'], 'quotes': data['quotes'],
            'first_seen': data['first_seen'], 'revisions':data['revisions'], 'source': ORIGIN,
            'smt_asset_id':data.get('smt_asset_id'),'smt_candles_15m':data.get('smt_candles_15m',[]),
            'smt_first_seen':data.get('smt_first_seen',{}),'smt_revisions':data.get('smt_revisions',[]),
            'provenance_verified': True, 'evidence_sha256': data['content_sha256'],
            'limitations': ['Collector attribution, not provider cryptographic attestation',
                           'First-observed candle versions and actual observed quotes only']}
