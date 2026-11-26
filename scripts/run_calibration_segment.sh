#!/usr/bin/env bash
# Only the offline PAPER calibration is run here. A complete prior is written
# by the Python runner only after the whole frozen dataset has been processed.
set -euo pipefail
mkdir -p calibration
args=(--days "$DAYS" --scan-minutes "$SCAN" --adaptive-exits "$EXITS"
      --dataset calibration/history.json.gz --checkpoint calibration/checkpoint.json.gz
      --history-cache .bzu-history-cache --checkpoint-every 100
      --max-seconds "${BZU_REPLAY_SEGMENT_SECONDS:-1800}"
      --status calibration/status.json --output calibration/meta_priors.json
      --report calibration/report.json --labels calibration/labels.json)
if [[ -f calibration/checkpoint.json.gz ]]; then
  [[ -f calibration/history.json.gz ]] || { echo 'Original frozen history missing'; exit 1; }
  args+=(--resume)
else
  [[ ! -f calibration/history.json.gz ]] || { echo 'Dataset without matching checkpoint; start a fresh run'; exit 1; }
  args+=(--end-ms "$HISTORY_END_MS")
fi
python -u calibrate_meta_model.py "${args[@]}"
python - <<'PY'
import json, os
from pathlib import Path
status=json.loads(Path('calibration/status.json').read_text())['status']
if status not in ('PAUSED','COMPLETE'):
    raise SystemExit('Unexpected replay status: '+status)
with open(os.environ['GITHUB_OUTPUT'],'a') as output:
    output.write('paused='+str(status=='PAUSED').lower()+'\n')
    output.write('complete='+str(status=='COMPLETE').lower()+'\n')
PY
