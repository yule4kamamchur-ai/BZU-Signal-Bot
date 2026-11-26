"""Optional OKX public ticker feed for the always-on worker. No credentials.

A disconnected, stale, crossed, foreign or out-of-order quote is never used.
REST remains the authoritative fallback. No private execution is performed here.
"""
from __future__ import annotations
import json
import math
import threading
import time

URL='wss://ws.okx.com:8443/ws/v5/public'


def parse_ticker(row, instrument):
    try:
        if row.get('instId')!=instrument:
            return None
        p,b,a,opening=(float(row[k]) for k in ('last','bidPx','askPx','open24h'))
        ts=int(row['ts'])
        volume=float(row.get('volCcy24h') or 0)
        bs=float(row.get('bidSz') or 0); ass=float(row.get('askSz') or 0)
        if not all(math.isfinite(v) for v in (p,b,a,opening,volume,bs,ass)) or not 0<b<=a or p<=0 or min(volume,bs,ass)<0:
            return None
        return {'price':p,'bid':b,'ask':a,'ts':ts,'change24h':(p/opening-1)*100 if opening>0 else 0.,
                'volume24h':volume,'bid_size':bs,'ask_size':ass,'source':'OKX','transport':'WEBSOCKET'}
    except (ValueError,TypeError,KeyError,OverflowError):
        return None


class PublicTicker:
    def __init__(self,instrument):
        self.instrument=instrument; self._latest=None; self._lock=threading.Lock()
        self._stop=threading.Event(); self._thread=None

    def ingest(self,message):
        if message in ('pong','ping'):
            return
        try:
            packet=json.loads(message)
            if packet.get('arg',{}).get('channel')!='tickers':
                return
            for row in packet.get('data',[]):
                quote=parse_ticker(row,self.instrument)
                if quote is None: continue
                now=int(time.time()*1000)
                if not 0<=now-quote['ts']<=5000: continue
                with self._lock:
                    if self._latest is None or quote['ts']>=self._latest['ts']:
                        self._latest=quote
        except (ValueError,TypeError,AttributeError):
            return

    def get(self,now=None):
        now=int(time.time()*1000) if now is None else now
        with self._lock:
            q=self._latest
            return dict(q) if q and 0<=now-q['ts']<=5000 else None

    def start(self):
        import websocket
        def loop():
            while not self._stop.is_set():
                ws=None
                try:
                    ws=websocket.create_connection(URL,timeout=10)
                    ws.send(json.dumps({'op':'subscribe','args':[{'channel':'tickers','instId':self.instrument}]}))
                    while not self._stop.is_set():
                        try:
                            message=ws.recv()
                        except websocket.WebSocketTimeoutException:
                            ws.send('ping'); message=ws.recv()
                        if not message: break
                        self.ingest(message)
                except (OSError,websocket.WebSocketException,ValueError):
                    pass
                finally:
                    with self._lock: self._latest=None
                    if ws:
                        try: ws.close()
                        except Exception: pass
                self._stop.wait(5)
        self._thread=threading.Thread(target=loop,name='bzu-okx-public-ticker',daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
