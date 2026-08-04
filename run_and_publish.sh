#!/usr/bin/env bash
# Fetch a reading, regenerate the dashboard data, and (optionally) deploy.
#
# Safe to call straight from cron on Linux or launchd on macOS: it resolves its
# own directory, finds its own interpreter, refuses to overlap with a previous
# run, and never assumes a login shell's PATH.
#
#   WG_SKIP_DEPLOY=1   fetch and regenerate only, don't push to Cloudflare
#   WG_PYTHON=...      explicit interpreter (default: ./venv/bin/python, else python3)
#   WG_LOG=...         append all output to this file as well as stdout
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# cron gives you a near-empty PATH; make sure the usual places are visible so
# python3, node/npx and flock resolve the same way they do in a shell.
export PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:${PATH:-}"

if [ -n "${WG_LOG:-}" ]; then
    mkdir -p "$(dirname "$WG_LOG")"
    exec >> "$WG_LOG" 2>&1
fi

# One run at a time. WaterGuru's API does a full Cognito login per run and the
# LLM step can take a while on a big model, so overlapping runs are a real risk.
if [ -z "${WG_LOCKED:-}" ] && command -v flock >/dev/null 2>&1; then
    export WG_LOCKED=1
    exec flock -n "$PWD/.run.lock" "$0" "$@"
fi

echo "=== waterguru run $(date '+%Y-%m-%d %H:%M:%S %Z') ==="

PYTHON="${WG_PYTHON:-}"
if [ -z "$PYTHON" ]; then
    if [ -x "./venv/bin/python" ]; then
        PYTHON="./venv/bin/python"
    else
        PYTHON="$(command -v python3)"
    fi
fi

"$PYTHON" fetch.py

if [ "${WG_SKIP_DEPLOY:-0}" = "1" ]; then
    echo "WG_SKIP_DEPLOY=1, skipping Cloudflare deploy"
    exit 0
fi

if ! command -v npx >/dev/null 2>&1; then
    echo "npx not found, skipping Cloudflare deploy (set WG_SKIP_DEPLOY=1 to silence)" >&2
    exit 0
fi

npx --yes wrangler pages deploy site --project-name waterguru-dashboard --branch main --commit-dirty=true
