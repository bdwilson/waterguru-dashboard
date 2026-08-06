#!/usr/bin/env bash
# One-time setup: create the Cloudflare Pages project run_and_publish.sh
# deploys to, using CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID from .env -
# no `wrangler login`, no browser, safe to run on a headless box.
#
#   ./cf_create.sh
#
# Safe to re-run: if the project already exists, this says so and exits 0
# rather than treating that as a failure - the point is "make sure the
# project exists," not "create it exactly once by hand."
#
#   WG_PYTHON=...   explicit interpreter (default: ./venv/bin/python, else python3)
set -euo pipefail

cd "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

PYTHON="${WG_PYTHON:-}"
if [ -z "$PYTHON" ]; then
    if [ -x "./venv/bin/python" ]; then
        PYTHON="./venv/bin/python"
    else
        PYTHON="$(command -v python3)"
    fi
fi

source "$PWD/cf_env.sh"

if [ -z "${CLOUDFLARE_API_TOKEN:-}" ] || [ -z "${CLOUDFLARE_ACCOUNT_ID:-}" ]; then
    echo "CLOUDFLARE_API_TOKEN and CLOUDFLARE_ACCOUNT_ID must be set - in .env, or already" >&2
    echo "exported in this shell. See .env.example." >&2
    exit 1
fi

if ! command -v npx >/dev/null 2>&1; then
    echo "npx not found - install Node.js, then re-run this." >&2
    exit 1
fi

echo "Creating Cloudflare Pages project '$CF_PROJECT'..."

# See run_and_publish.sh for why this is `set +e` / capture / `set -e`
# rather than `if ! cmd; then exit "$?"` - that pattern silently always
# exits 0, since $? in that `then` reflects the negated condition, not cmd's
# real status.
set +e
create_output=$(npx --yes wrangler pages project create "$CF_PROJECT" --production-branch main 2>&1)
create_status=$?
set -e

echo "$create_output"
echo ""

if [ "$create_status" -eq 0 ]; then
    echo "Done. Deploy with ./run_and_publish.sh."
elif echo "$create_output" | grep -qi "already exists"; then
    echo "'$CF_PROJECT' already exists - nothing to do. Deploy with ./run_and_publish.sh."
else
    echo "Project creation failed - see the wrangler output above." >&2
    exit "$create_status"
fi
