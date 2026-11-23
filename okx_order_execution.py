"""Optional OKX execution for BZU. Exchange fills are separate from the paper ledger."""
from base64 import b64encode
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import json
import os
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

import bzu_execution_rules as rules
import position_monitor


class ExecutionError(ValueError):
    """Operational error safe for reports and logs."""


class OKXRejected(ExecutionError):
    def __init__(self, code):
        self.code = str(code)
        super().__init__('OKX_RESPONSE_' + (self.code if self.code.isdecimal() else 'UNKNOWN'))


def decimal(value):
    number = Decimal(str(value))
    if not number.is_finite():
        raise ExecutionError('OKX_NONFINITE_VALUE')
    return number


def amount(value):
    return format(decimal(value).normalize(), 'f')


def client_id(prefix, key):
    return prefix + hashlib.sha256(key.encode()).hexdigest()[:32-len(prefix)]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, target):
        return None


class OKX:
    def __init__(self, mode):
        self.mode = mode
        self.opener = build_opener(NoRedirect())
        self.key, self.secret, self.passphrase = (
            os.getenv(name) for name in ('OKX_API_KEY','OKX_API_SECRET','OKX_API_PASSPHRASE'))
        if not all((self.key,self.secret,self.passphrase)):
            raise ExecutionError('OKX_CREDENTIALS_MISSING')
        self.base = os.getenv('OKX_TRADE_HOST') or 'https://www.okx.com'
        if self.base not in {'https://www.okx.com','https://app.okx.com',
                             'https://my.okx.com','https://eea.okx.com','https://us.okx.com'}:
            raise ExecutionError('OKX_INVALID_TRADE_HOST')

    def get(self, path, **query):
        return self.request('GET', path+'?'+urlencode(query) if query else path)

    def post(self, path, payload, *, expiry=None):
        return self.request('POST',path,payload,expiry=expiry)

    def request(self, method, path, payload=None, *, expiry=None):
        timestamp = datetime.now(timezone.utc).isoformat(timespec='milliseconds').replace('+00:00','Z')
        data = json.dumps(payload,separators=(',',':')) if payload is not None else ''
        signature = hmac.digest(self.secret.encode(), (timestamp+method+path+data).encode(), 'sha256')
        headers = {'OK-ACCESS-KEY':self.key, 'OK-ACCESS-PASSPHRASE':self.passphrase,
                   'OK-ACCESS-SIGN':b64encode(signature).decode(), 'OK-ACCESS-TIMESTAMP':timestamp,
                   'Content-Type':'application/json','User-Agent':'BZU/14.4'}
        if self.mode == 'DEMO':
            headers['x-simulated-trading'] = '1'
        if expiry is not None:
            headers['expTime'] = str(expiry)
        request = Request(self.base+path, data.encode() if data else None, headers, method=method)
        try:
            with self.opener.open(request, timeout=7) as response:
                result = json.load(response)
        except HTTPError as exc:
            raise ExecutionError(f'OKX_HTTP_{exc.code}') from None
        except (URLError, TimeoutError, ValueError, OSError):
            raise ExecutionError('OKX_RESPONSE_UNCERTAIN') from None
        rows = result.get('data') or []
        code = next((str(row['sCode']) for row in rows if row.get('sCode') not in (None,'','0',0)),
                    str(result.get('code','UNKNOWN')))
        if code != '0':
            raise OKXRejected(code)
        return rows


def contract_size(api, instrument, plan, margin):
    rows = api.get('/api/v5/public/instruments', instType='SWAP', instId=instrument)
    spec = rows[0] if rows else {}
    if spec.get('ctType') != 'linear' or spec.get('ctValCcy') != instrument.split('-')[0]:
        raise ExecutionError('OKX_CONTRACT_UNSUPPORTED')
    step = decimal(spec['lotSz'])
    ct_value = decimal(spec['ctVal'])
    if min(step, ct_value) <= 0:
        raise ExecutionError('OKX_CONTRACT_GEOMETRY_INVALID')
    limits = api.get('/api/v5/account/max-size', instId=instrument, tdMode=margin, px=amount(plan['entry']))
    if not limits:
        raise ExecutionError('OKX_ORDER_SIZE_UNAVAILABLE')
    cap = decimal(limits[0]['maxBuy' if plan['side']==1 else 'maxSell'])
    order_cap = spec.get('maxMktSz' if plan['order_type']=='MARKET' else 'maxLmtSz')
    if order_cap:
        cap = min(cap,decimal(order_cap))
    notional = (os.getenv('OKX_ORDER_NOTIONAL_USDT') or '').strip().upper()
    budget = (os.getenv('OKX_RISK_USDT') or '').strip()
    if bool(notional) == bool(budget):
        raise ExecutionError('SET_ONE_OKX_SIZE_POLICY')
    if notional == 'MAX':
        raw = cap
    elif notional:
        raw = decimal(notional)/(decimal(plan['entry'])*ct_value)
    else:
        # Budget the cost-inclusive distance. Stop-market fills may still gap.
        fee = decimal(plan.get('exit_fee_rate',0))+decimal(plan.get('entry_fee_rate',0))
        slippage = decimal(plan.get('slippage_bps',0))/10_000
        raw = decimal(budget)/(ct_value*(decimal(plan['risk'])+decimal(plan['entry'])*(fee+slippage)))
    size = (raw//step)*step
    if size < decimal(spec['minSz']) or size > cap:
        raise ExecutionError('OKX_SIZE_OUTSIDE_AVAILABLE_RANGE')
    return amount(size)


def bracket(stop, tp):
    return {'slTriggerPx':amount(stop),'slOrdPx':'-1','slTriggerPxType':'last',
            'tpTriggerPx':amount(tp),'tpOrdPx':'-1','tpTriggerPxType':'last'}


def _lookup(api, inst, ord_id):
    try:
        rows=api.get('/api/v5/trade/order',instId=inst,clOrdId=ord_id)
    except OKXRejected as exc:
        if exc.code != '51603':
            raise
        return None
    return rows[0] if rows else None


def _positions(api, inst):
    return [row for row in api.get('/api/v5/account/positions',instId=inst)
            if decimal(row.get('pos') or '0') != 0]


def _algos(api, inst):
    return [row for kind in ('oco','conditional')
            for row in api.get('/api/v5/trade/orders-algo-pending',instId=inst,ordType=kind)]


def _close(api, inst, tracked, snap, size, reason):
    plan = tracked['plan']
    payload = {'instId':inst,'tdMode':tracked['margin'],'posSide':'net','ordType':'market',
               'side':'sell' if plan['side']==1 else 'buy','reduceOnly':True,'sz':amount(size),
               'clOrdId':client_id('BZC',tracked['client_id']+str(snap.now//60_000))}
    api.post('/api/v5/trade/order',payload,expiry=snap.now+20_000)
    tracked.update(status='CLOSE_REQUESTED', reason=reason)


def _guard(api, inst, tracked, position, parent, snap, cfg, engine):
    plan = tracked['plan']
    size = decimal(position['pos'])
    if position.get('posSide') != 'net' or plan['side']*size <= 0:
        raise ExecutionError('OKX_POSITION_DOES_NOT_MATCH_PLAN')
    size = abs(size)
    attached = [row for row in _algos(api,inst) if row.get('algoClOrdId') in
                (tracked['algo_client_id'],tracked['fallback_algo_id'])]
    if len(attached)>1:
        raise ExecutionError('OKX_MULTIPLE_PROTECTIVE_ORDERS')
    quote = snap.bid if plan['side']==1 else snap.ask
    fresh = snap.trusted and 0 <= snap.now-snap.ticker_ts <= 20_000 and 0 < snap.bid <= snap.ask
    if not attached:
        fill_update = int(parent.get('uTime') or parent.get('fillTime') or 0)
        if parent['state']=='filled' and fill_update and snap.now-fill_update<15_000:
            tracked['status']='ATTACHED_BRACKET_RECONCILING'
            return
        if not fresh:
            raise ExecutionError('OKX_POSITION_PROTECTION_UNCONFIRMED')
        if min(plan['side']*(quote-plan['stop']),plan['side']*(plan['tp']-quote)) <= 0:
            _close(api,inst,tracked,snap,size,'PROTECTION_LEVEL_ALREADY_CROSSED')
            return
        api.post('/api/v5/trade/order-algo',{
            'instId':inst,'tdMode':tracked['margin'],'posSide':'net',
            'side':'sell' if plan['side']==1 else 'buy','ordType':'oco','reduceOnly':True,
            'sz':amount(size),'algoClOrdId':tracked['fallback_algo_id'],**bracket(plan['stop'],plan['tp'])})
        tracked.update(status='BRACKET_SUBMITTED')
        return
    algo=attached[0]
    if decimal(algo.get('sz') or '0') != size:
        raise ExecutionError('OKX_BRACKET_SIZE_DOES_NOT_MATCH_POSITION')
    stop = float(algo['slTriggerPx'])
    target = float(algo['tpTriggerPx'])
    if min(stop,target) <= 0:
        raise ExecutionError('OKX_INCOMPLETE_BRACKET')
    tracked.update(status='PROTECTED',stop=stop,target=target)
    if not fresh:
        return
    fill = float(parent.get('avgPx') or position['avgPx'])
    filled_ts = int(parent.get('fillTime') or parent.get('uTime') or position['cTime'])
    risk = plan['side']*(fill-plan['initial_stop'])
    if risk <= 0 or plan['side']*(target-fill) <= 0:
        _close(api,inst,tracked,snap,size,'INVALID_EXCHANGE_FILL_GEOMETRY')
        return
    active = {**plan, **tracked.get('monitor',{}), 'entry':fill,'stop':stop,
              'risk':risk,'filled_ts':filled_ts,'breakeven_armed':False,
              'exit_deadline':filled_ts+plan.get('max_hold_minutes',cfg.max_hold_minutes)*60_000}
    for bar in snap.candles.get('1m',[]):
        if bar.confirmed and filled_ts <= bar.ts and bar.ts+60_000 <= snap.ticker_ts:
            peak, adverse = (bar.high,bar.low) if plan['side']==1 else (bar.low,bar.high)
            position_monitor.track(active,bar.ts+60_000,peak,adverse)
    position_monitor.track(active,snap.ticker_ts,quote,quote)
    position_monitor.observe_structure(active,snap,snap.ticker_ts,cfg,engine)
    tracked['monitor'] = {k:active[k] for k in active if k.startswith((
        'max_favorable','max_adverse','protection_structure','adverse_close_pair'))}
    guard = position_monitor.evaluate_active_trade_lifecycle(active,snap.ticker_ts,quote,cfg,engine,snapshot=snap)
    mfe = position_monitor.mfe_protection(active,snap.ticker_ts,quote,cfg,engine)
    if guard['should_exit'] or (mfe and mfe['exit_now']):
        reason = guard['exit_reason'] or 'MFE_PROTECTIVE_EXIT'
        _close(api,inst,tracked,snap,size,reason)
        return
    choices = [price for price in (guard['stop'],(mfe or {}).get('price')) if price is not None
               and plan['side']*(price-stop)>1e-8]
    if choices:
        new_stop=max(choices,key=lambda price: plan['side']*price)
        api.post('/api/v5/trade/amend-algos',{'instId':inst,'algoId':algo['algoId'],
                 'newSlTriggerPx':amount(new_stop),'newSlOrdPx':'-1','cxlOnFail':False})
        tracked.update(status='BRACKET_AMEND_SUBMITTED', requested_stop=new_stop)


def synchronize(api, state, book, result, snap, cfg, engine, persist):
    """Own one instrument order. Recheck trade state before any retry or replacement."""
    inst = cfg.instrument
    tracked = book.get('order')
    if tracked is not None:
        parent = _lookup(api, inst, tracked['client_id'])
        live = _positions(api,inst)
        if parent is None:
            if not live and snap.now>=tracked['plan']['expires_ts']:
                book.pop('order')
            else:
                tracked['status'] = 'ENTRY_RECONCILING'
            return
        if parent['state'] in ('live','partially_filled'):
            plan=tracked['plan']
            waiting = next((p for p in engine.pending_plans(state)
                            if p['id']==plan['id']),None)
            cancellation = any(e.get('order_id')==plan['id'] and e['kind'] in ('CANCELLED','EXPIRED','CANCEL_REQUESTED')
                               for e in result.get('events',[]))
            if (live or snap.now>=plan['expires_ts'] or cancellation or (waiting and waiting.get('cancel_requested_ts'))
                    or (rules.early_ny(snap.now) and not plan.get('opening_sweep_confirmed'))):
                api.post('/api/v5/trade/cancel-order',{'instId':inst,'clOrdId':tracked['client_id']})
                tracked['status'] = 'CANCELLATION_REQUESTED'
                parent = _lookup(api,inst,tracked['client_id']) or parent
                if parent['state'] in ('live','partially_filled'):
                    return
                live = _positions(api,inst)
            else:
                tracked['status'] = 'PENDING'
                return
        if live:
            if len(live)!=1 or decimal(parent.get('accFillSz') or '0') < abs(decimal(live[0]['pos'])):
                raise ExecutionError('OKX_UNRELATED_POSITION')
            _guard(api,inst,tracked,live[0],parent,snap,cfg,engine)
            return
        terminal_update = int(parent.get('uTime') or parent.get('fillTime') or 0)
        if parent['state']=='filled' and terminal_update and snap.now-terminal_update<15_000:
            tracked['status']='FILLED_POSITION_RECONCILING'
            return
        own_algos=[row for row in _algos(api,inst) if row.get('algoClOrdId') in
                   (tracked['algo_client_id'],tracked['fallback_algo_id'])]
        if own_algos:
            api.post('/api/v5/trade/cancel-algos',[{'instId':inst,'algoId':row['algoId']} for row in own_algos])
            tracked['status'] = 'FLAT_ALGO_CANCELLATION_REQUESTED'
            return
        book['last_order']={'id':tracked['client_id'],'exchange_state':parent['state'],'ts':snap.now}
        book.pop('order')
        return

    plan = next((p for p in result.get('selected_plans',[]) if engine.bzu_policy.full_position(p)),None)
    if not plan or not snap.trusted or not 0 <= snap.now-snap.ticker_ts <= 20_000:
        return
    account = api.get('/api/v5/account/config')
    if not account or account[0].get('posMode') != 'net_mode':
        raise ExecutionError('OKX_NET_MODE_REQUIRED')
    if _positions(api,inst) or api.get('/api/v5/trade/orders-pending',instId=inst) or _algos(api,inst):
        raise ExecutionError('OKX_INSTRUMENT_NOT_FLAT')
    margin = (os.getenv('OKX_MARGIN_MODE') or 'cross').strip().lower()
    if margin not in ('cross','isolated'):
        raise ExecutionError('OKX_INVALID_MARGIN_MODE')
    order_id=client_id('BZE',plan['event_key'])
    algo_id=client_id('BZA',plan['event_key'])
    fallback=client_id('BZF',plan['event_key'])
    if _lookup(api,inst,order_id) is not None:
        return
    market=plan['order_type']=='MARKET'
    if not market and (plan['side']*((snap.ask if plan['side']==1 else snap.bid)-plan['entry'])<=0):
        return
    size = contract_size(api,inst,plan,margin)
    payload={'instId':inst,'tdMode':margin,'posSide':'net','side':'buy' if plan['side']==1 else 'sell',
             'ordType':'market' if market else 'post_only','clOrdId':order_id,'sz':size,
             'attachAlgoOrds':[{'attachAlgoClOrdId':algo_id,**bracket(plan['stop'],plan['tp'])}]}
    if not market:
        payload['px']=amount(plan['entry'])
    deadline=min(plan['expires_ts'],snap.now+20_000)
    if deadline <= snap.now:
        return
    book['order']={'plan':deepcopy(plan),'client_id':order_id,'algo_client_id':algo_id,'fallback_algo_id':fallback,
                   'margin':margin,'status':'ENTRY_REQUESTED'}
    persist()
    api.post('/api/v5/trade/order',payload,expiry=deadline)
    book['order']['status']='ENTRY_ACKNOWLEDGED'
    synchronize(api,state,book,{'events':[]},snap,cfg,engine,persist)


def run(state, result, snap, cfg, engine, persist):
    mode = (os.getenv('BZU_OKX_EXECUTION') or 'PAPER').strip().upper()
    if mode=='PAPER':
        if (state.get('okx_execution') or {}).get('order'):
            return {'mode':'PAPER','status':'EXCHANGE_ORDER_REQUIRES_RECONCILIATION'}
        return {'mode':'PAPER','status':'SIMULATION'}
    if mode not in ('DEMO','LIVE') or cfg.instrument!='BZ-USDT-SWAP':
        return {'mode':mode,'status':'OKX_CONFIGURATION_INVALID'}
    book=state.setdefault('okx_execution',{'mode':mode})
    if book.get('mode')!=mode:
        if book.get('order'):
            return {'mode':mode,'status':'OKX_MODE_CHANGED_WITH_TRACKED_ORDER'}
        book.update(mode=mode)
    try:
        synchronize(OKX(mode),state,book,result,snap,cfg,engine,persist)
        order=book.get('order') or {}
        return {'mode':mode,'status':order.get('status','FLAT'),'stop':order.get('stop'),
                'target':order.get('target')}
    except (ExecutionError,InvalidOperation,KeyError,TypeError) as exc:
        return {'mode':mode,'status':str(exc) if isinstance(exc,ExecutionError) else 'OKX_RESPONSE_UNUSABLE'}
