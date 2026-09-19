#!/usr/bin/env bash
# Weekly quick snapshot of the small durable state files, verified by the candidate's own checks.
# state.db is excluded from the size-capped quick path deliberately: the host backup system owns it.
set -euo pipefail
hermes backup --quick --keep 8
latest="$(ls -td "${HERMES_HOME:-$HOME/.hermes}"/state-snapshots/*/ 2>/dev/null | head -1)"
[[ -n "$latest" && -f "$latest/manifest.json" ]] || { echo "[CRON_FAILURE] no snapshot manifest"; exit 1; }
python3 -c "import json,sys; m=json.load(open(sys.argv[1])); bad=m.get('failed_dbs') or []; print('snapshot', sys.argv[1], 'files', len(m.get('files',{})), 'failed_dbs', bad); sys.exit(1 if bad else 0)" "$latest/manifest.json" || { echo "[CRON_FAILURE] snapshot had failed database copies"; exit 1; }
