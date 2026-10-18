"""Verified-content OKX history bundles, separate from observed live quotes."""
from __future__ import annotations

import time
from urllib.parse import urlparse, parse_qs

import research_evidence

SCHEMA='controlled_okx_history_v1'


def download(instrument,days,end):
    import research_validate
    traces=[]
    def fetch(url):
        body=research_validate.request_json(url)
        traces.append({'url':url,'received_ts':int(time.time()*1000),
                       'response':body,'response_sha256':research_evidence.digest(body)})
        return body
    data=research_validate.download(instrument,days,end,fetch=fetch)
    bundle={'schema':SCHEMA,'source':'OKX_PUBLIC_HTTP_HISTORY_COLLECTOR',
            'data':data,'requests':traces,
            'execution_mode':'HISTORICAL_CANDLES_WITH_EXPLICIT_ESTIMATED_QUOTES',
            'provenance_limit':'Recorded HTTP responses; not an exchange cryptographic signature'}
    return research_evidence.seal(bundle)


def verify(bundle,engine):
    if bundle.get('schema')!=SCHEMA or bundle.get('source')!='OKX_PUBLIC_HTTP_HISTORY_COLLECTOR':
        raise ValueError('Unverified history bundle')
    if bundle.get('content_sha256')!=research_evidence.digest({k:v for k,v in bundle.items() if k!='content_sha256'}):
        raise ValueError('History bundle checksum differs')
    data=bundle['data'];available={}
    instrument_metadata=[]
    if data.get('quotes') or data.get('first_seen'):
        raise ValueError('Reconstructed history must not claim observed live quotes')
    for request in bundle['requests']:
        url=urlparse(request['url'])
        if url.scheme!='https' or url.netloc!='www.okx.com' or url.path not in (
                '/api/v5/market/history-candles','/api/v5/public/instruments'):
            raise ValueError('Unapproved market history origin')
        body=request['response']
        if request['response_sha256']!=research_evidence.digest(body) or str(body.get('code'))!='0':
            raise ValueError('Invalid history source response')
        if url.path.endswith('history-candles'):
            params=parse_qs(url.query);tf=params['bar'][0];instrument=params['instId'][0]
            for raw in body['data']:
                c=engine.Candle.parse(raw)
                if c.confirmed:
                    available[(instrument,tf,c.ts)]=c
        else:
            instrument_metadata+=body.get('data',[])
    metadata=next((r for r in instrument_metadata if r.get('instId')==data['instrument']),None)
    if not metadata or engine.finite(metadata.get('tickSz',0))!=data['tick'] or data['tick']<=0:
        raise ValueError('Missing or mismatched source tick metadata')
    for tf,rows in data['candles'].items():
        for raw in rows:
            c=engine.Candle.parse(raw)
            if available.get((data['instrument'],tf,c.ts))!=c:
                raise ValueError('Candle lacks a matching recorded source response')
            if not c.confirmed or c.ts+engine.TF[tf]>data['end_ts']:
                raise ValueError('Unclosed/future candle in research history')
    for raw in data.get('smt_candles_15m',[]):
        c=engine.Candle.parse(raw)
        if available.get((data.get('smt_asset_id'),'15m',c.ts))!=c:
            raise ValueError('SMT candle lacks matching source provenance')
    return {'status':'VERIFIED_RECORDED_HTTP_CONTENT','requests':len(bundle['requests']),
            'fingerprint':bundle['content_sha256'],'observed_live_quotes':False,
            'provider_signature':False}
