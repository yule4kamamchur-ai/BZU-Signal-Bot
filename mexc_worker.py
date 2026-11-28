"""Independent always-on MEXC worker; serves the current atomic JSON directly.
Run on an always-on Linux host (or Docker), not GitHub-hosted cron.
"""
from __future__ import annotations
import json
import fcntl
import subprocess
import sys
import logging
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Thread
from urllib.parse import urlsplit
from mexc_multi_scanner import _atomic_write


def serve(root, state_path, bind, port):
    routes={'/':(root/'index.html','text/html; charset=utf-8'),'/index.html':(root/'index.html','text/html; charset=utf-8'),
        '/multi_scanner.js':(root/'multi_scanner.js','text/javascript; charset=utf-8'),'/live_terminal.css':(root/'live_terminal.css','text/css; charset=utf-8'),
        '/multi_scanner_state.json':(state_path,'application/json; charset=utf-8')}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self): self.respond(False)
        def do_HEAD(self): self.respond(True)
        def respond(self,head):
            path=urlsplit(self.path).path
            if path=='/healthz':
                try:
                    data=json.loads(state_path.read_text()); health=data.get('health',{}); interval=int(health.get('expected_interval_sec',180))
                    age=(time.time()*1000-int(data.get('updated_ts',0)))/1000
                    ok=0<=age<=interval+60 and health.get('status') in ('OK','DEGRADED')
                    content=json.dumps({'ok':ok,'age_sec':round(age,1),'status':health.get('status'),'last_attempt_ts':data.get('last_attempt_ts')}).encode()
                except Exception:
                    ok=False; content=b'{"ok":false,"status":"NO_STATE"}'
                self.send_response(200 if ok else 503); self.send_header('Content-Type','application/json'); self.send_header('Cache-Control','no-store'); self.send_header('Content-Length',str(len(content))); self.end_headers()
                if not head: self.wfile.write(content)
                return
            item=routes.get(path)
            if not item: self.send_error(404); return
            try: content=item[0].read_bytes()
            except OSError: self.send_error(503); return
            self.send_response(200); self.send_header('Content-Type',item[1]); self.send_header('Cache-Control','no-store, max-age=0'); self.send_header('Content-Length',str(len(content))); self.send_header('X-Content-Type-Options','nosniff'); self.end_headers()
            if not head: self.wfile.write(content)
        def log_message(self,*_): pass
    server=ThreadingHTTPServer((bind,port),Handler)
    Thread(target=server.serve_forever,daemon=True).start()
    return server


def main():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    root=Path(__file__).resolve().parent
    data=Path(os.getenv('MEXC_DATA_DIR',str(root/'data'))); data.mkdir(parents=True,exist_ok=True)
    state=data/'multi_scanner_state.json'; memory=data/'multi_scanner_memory.json'
    interval=int(os.getenv('MEXC_SCAN_INTERVAL_SEC','60'))
    if interval<60: raise ValueError('MEXC_SCAN_INTERVAL_SEC must be at least 60')
    # Never silently create an empty journal while migrating an existing bot.
    if not memory.exists() and os.getenv('BZU_ALLOW_EMPTY_LEDGER')!='1':
        raise RuntimeError('Copy latest multi_scanner_memory.json into MEXC_DATA_DIR, or explicitly set BZU_ALLOW_EMPTY_LEDGER=1 for a NEW ledger')
    if memory.exists(): json.loads(memory.read_text())
    worker_lock=open(data/'.worker.lock','a')
    fcntl.flock(worker_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    deadline=int(os.getenv('MEXC_CYCLE_TIMEOUT_SEC',str(min(150,interval-10))))
    if not 10<=deadline<interval: raise ValueError('Cycle timeout must be below interval')
    os.environ['MEXC_SCAN_INTERVAL_SEC']=str(interval)
    server=serve(root,state,os.getenv('LIVE_HTTP_BIND','0.0.0.0'),int(os.getenv('LIVE_HTTP_PORT','8080')))
    logging.info('MEXC worker ready; interval=%ds HTTP=%s',interval,server.server_address)
    next_run=time.monotonic()
    while True:
        time.sleep(max(0,next_run-time.monotonic()))
        started=time.monotonic()
        try:
            subprocess.run([sys.executable,str(root/'mexc_multi_scanner.py'),'--output',str(state),'--memory',str(memory),'--notify'],check=True,timeout=deadline,cwd=root)
        except Exception as exc:
            logging.exception('MEXC cycle failed; ledger not reset')
            try:
                previous=json.loads(state.read_text()) if state.exists() else {'updated_ts':0,'updated_kyiv':'','instruments':[]}
                previous['last_attempt_ts']=int(time.time()*1000)
                previous.setdefault('health',{}).update(status='FAILED_CYCLE',last_error=type(exc).__name__,expected_interval_sec=interval)
                _atomic_write(state,previous)
            except Exception: logging.exception('Cannot publish failure heartbeat')
        duration=time.monotonic()-started
        if duration>interval: logging.warning('MEXC scan took %.1fs, longer than interval %ds',duration,interval)
        # No overlapping cycles and no catch-up storm. Next start is interval seconds after previous start unless overrun.
        next_run=max(started+interval,time.monotonic()+1)


if __name__=='__main__': main()
