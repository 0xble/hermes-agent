#!/usr/bin/env bash
# The unprivileged CI user runs Electron with its packaged Linux sandbox enabled.
set -euo pipefail

if [ "$(uname -s)" != Linux ] || [ "$(id -u)" -ne 0 ]; then
  echo 'Electron sandbox preparation requires root in the disposable Linux CI container.' >&2
  exit 1
fi

node scripts/build/electron-runtime.mjs --source "$PWD" --no-install
sandbox=$(node --input-type=module - <<'JS'
import { createRequire } from 'node:module'
import { dirname, join } from 'node:path'
const require = createRequire(join(process.cwd(), 'apps/desktop/package.json'))
console.log(join(dirname(require.resolve('electron/package.json')), 'dist/chrome-sandbox'))
JS
)
test -f "$sandbox"
# The exact gate prepares dependencies again under a fresh home. That can replace
# node_modules and erase setuid ownership, so keep the same locked runtime outside it.
runtime="$PWD/.ci/electron-sandbox"
mkdir -p "$runtime"
cp -a "$(dirname "$sandbox")/." "$runtime/"
chown -R root:root "$runtime"
chmod 4755 "$runtime/chrome-sandbox"
