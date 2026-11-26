#!/usr/bin/env python3
"""Persistent worker: scan on 3s ticks and report once per 15m bucket.

Run INSTEAD of scheduled GitHub cycles when an always-on host is available.
The one-shot GitHub workflow scans on its own five-minute schedule.
"""
from __future__ import annotations
import logging
import os
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from urllib.parse import urlsplit
from pathlib import Path

SCAN_MS=3000
REPORT_MS=900000
SETTLE_MS=5000


def due(now, checkpoint):
    scan=(now-SETTLE_MS)//SCAN_MS
    report=(now-SETTLE_MS)//REPORT_MS
    notify=report>checkpoint.get('report_bucket',-1)
    return scan>checkpoint.get('scan_bucket',-1) or notify, notify


def step(now, checkpoint, run_cycle):
    run,notify=due(now,checkpoint)
    if not run:
        return checkpoint,False
    started = time.monotonic()
    status=run_cycle(notify=notify)
    runtime = time.monotonic()-started
    if runtime > SCAN_MS/1000:
        logging.warning('3s scan finished in %.1fs', runtime)
    if status not in (0,2):
        raise RuntimeError('Bot cycle failed; checkpoint not advanced')
    updated={**checkpoint,'scan_bucket':(now-SETTLE_MS)//SCAN_MS,'last_cycle_ts':now}
    if notify:
        updated['report_bucket']=(now-SETTLE_MS)//REPORT_MS
    return updated,True


def serve_terminal(root, live_path, port):
    files = {
        '/': (root/'index.html', 'text/html; charset=utf-8'),
        '/index.html': (root/'index.html', 'text/html; charset=utf-8'),
        '/live_terminal.js': (root/'live_terminal.js', 'text/javascript; charset=utf-8'),
        '/live_terminal.css': (root/'live_terminal.css', 'text/css; charset=utf-8'),
        '/live_state.json': (live_path, 'application/json'),
    }

    class TerminalHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_file()

        def do_HEAD(self):
            self.send_file(head=True)

        def send_file(self, head=False):
            route = files.get(urlsplit(self.path).path)
            if not route:
                self.send_error(404)
                return
            path, mime = route
            try:
                data = path.read_bytes()
            except FileNotFoundError:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', mime)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            if not head:
                self.wfile.write(data)

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer((os.getenv('LIVE_HTTP_BIND', '127.0.0.1'), port), TerminalHandler)
    Thread(target=server.serve_forever, daemon=True).start()
    return server


def main():
    os.environ['ANALYSIS_SCAN_MINUTES']='3'
    os.environ['BZU_ENTRY_MODE']='HYBRID'
    os.environ['BZU_WORKER_INTERVAL_MS']=str(SCAN_MS)
    import bot_oneshot as core
    import full_limit_engine as engine
    core._verify_runtime(engine)
    if not core.TELEGRAM_TOKEN or not core.TELEGRAM_CHAT_ID:
        raise ValueError('Configure TELEGRAM_TOKEN and TELEGRAM_CHAT_ID before starting worker')
    state_path=Path(core.STATE_FILE)
    checkpoint_path=Path(os.environ.get('WORKER_CHECKPOINT_FILE',str(state_path.with_name('worker_schedule_v12_9.json'))))
    if checkpoint_path.resolve()==state_path.resolve():
        raise ValueError('Worker checkpoint must differ from trade ledger')
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(message)s')
    os.environ.setdefault('LIVE_STATE_FILE', str(state_path.with_name('live_state.json')))
    market_cache = {}
    if os.getenv('BZU_PUBLIC_WS','false').lower() in ('1','true','yes'):
        try:
            from okx_public_feed import PublicTicker
            market_cache['public_ticker']=PublicTicker(core.OKX_INST_ID).start()
        except (ImportError,OSError):
            logging.exception('Public WebSocket unavailable; using REST ticker')
    with engine.locked(Path(str(state_path)+'.worker.lock')):
        port = int(os.getenv('LIVE_HTTP_PORT','0'))
        if port:
            serve_terminal(Path(__file__).resolve().parent, Path(os.environ['LIVE_STATE_FILE']), port)
        checkpoint=engine.read_json(checkpoint_path) or {}
        ledger=engine.read_json(state_path) or {}
        queued=ledger.get('last_report_requested_ts',0)
        start = int(time.time()*1000)
        current, elapsed = divmod(start, REPORT_MS)
        # Starting mid-quarter must not produce an extra regular report.
        floor = current-1 if elapsed < 30_000 else current
        checkpoint['report_bucket'] = max(checkpoint.get('report_bucket',-1),queued//REPORT_MS,floor)
        while True:
            now=int(time.time()*1000)
            try:
                updated,ran=step(now,checkpoint,lambda notify:engine.run_live(
                    core, auto_report=True, report_allowed=notify, market_cache=market_cache))
                if ran:
                    if updated.get('report_bucket') != checkpoint.get('report_bucket'):
                        engine.atomic_write(checkpoint_path,updated)
                    checkpoint=updated
            except Exception:
                market_cache.pop('runtime_state', None)
                logging.exception('Cycle failed; preserving ledger and retrying in 15 seconds')
                time.sleep(15)
                continue
            time.sleep(.2)


if __name__=='__main__':
    main()
