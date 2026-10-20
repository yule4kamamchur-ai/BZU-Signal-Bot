#!/usr/bin/env python3
"""Optional single-host worker: closed-bar scan each 3m, report each 15m.

Run INSTEAD of scheduled GitHub cycles when an always-on host is available.
The one-shot GitHub workflow remains supported and unchanged in cadence.
"""
from __future__ import annotations
import logging
import os
import time
from pathlib import Path

SCAN_MS=180000
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
    status=run_cycle(notify=notify)
    if status not in (0,2):
        raise RuntimeError('Bot cycle failed; checkpoint not advanced')
    updated={**checkpoint,'scan_bucket':(now-SETTLE_MS)//SCAN_MS,'last_cycle_ts':now}
    if notify:
        # 2 means delivery failed AFTER durable queuing; avoid generating a
        # new report every second. The next report retries the saved outbox.
        updated['report_bucket']=(now-SETTLE_MS)//REPORT_MS
    return updated,True


def main():
    os.environ['ANALYSIS_SCAN_MINUTES']='3'
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
    with engine.locked(Path(str(state_path)+'.worker.lock')):
        checkpoint=engine.read_json(checkpoint_path) or {}
        ledger=engine.read_json(state_path) or {}
        queued=ledger.get('last_report_requested_ts',0)
        if queued:
            checkpoint['report_bucket']=max(checkpoint.get('report_bucket',-1),(queued-SETTLE_MS)//REPORT_MS)
        while True:
            now=int(time.time()*1000)
            try:
                updated,ran=step(now,checkpoint,lambda notify:engine.run_live(core,notify=notify))
                if ran:
                    engine.atomic_write(checkpoint_path,updated)
                    checkpoint=updated
            except Exception:
                logging.exception('Cycle failed; preserving ledger and retrying in 15 seconds')
                time.sleep(15)
                continue
            time.sleep(1)


if __name__=='__main__':
    main()
