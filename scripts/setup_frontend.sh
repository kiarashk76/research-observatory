#!/usr/bin/env bash
# Build local editor assets and install the browser used for Notes PDF exports.
set -euo pipefail
cd "$(dirname "$0")/.."
OBS_NODE="${OBS_NODE:-$(command -v node || true)}"
if [[ -z "$OBS_NODE" ]]; then
  echo 'Node.js 20+ and npm are required. Install them, then rerun this script.' >&2
  exit 1
fi
"$OBS_NODE" -e 'if(Number(process.versions.node.split(".")[0])<20)process.exit(1)' || {
  echo 'Node.js 20+ is required.' >&2; exit 1;
}
if [[ -n "${OBS_NPM_CLI:-}" ]]; then
  "$OBS_NODE" "$OBS_NPM_CLI" ci --prefix frontend/research-notes
else
  npm ci --prefix frontend/research-notes
fi
"$OBS_NODE" frontend/research-notes/build.mjs
mkdir -p runtime/tools/node/bin
if [[ ! -x runtime/tools/node/bin/node ]]; then
  ln -s "$(realpath "$OBS_NODE")" runtime/tools/node/bin/node
fi
if [[ "${1:-}" != '--skip-browser' ]]; then
  PLAYWRIGHT_BROWSERS_PATH="$PWD/runtime/tools/playwright" \
    "$OBS_NODE" frontend/research-notes/node_modules/playwright/cli.js install chromium
fi
echo 'Local editor assets are ready. Browser/tool files remain private under runtime/.'
