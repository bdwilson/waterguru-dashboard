#!/usr/bin/env bash
# Shared by run_and_publish.sh and cf_create.sh - not meant to be run directly.
#
# Bridges CLOUDFLARE_API_TOKEN / CLOUDFLARE_ACCOUNT_ID / WG_CF_PROJECT_NAME
# from .env into the current shell. wrangler is a separate (non-Python)
# process, so anything a Python process loaded into its own os.environ never
# reaches it - .env has to be bridged into *this shell* explicitly. Reuses
# envfile.py's own parser rather than `source .env` directly: several
# settings (e.g. WG_POOL_CONTEXT) are free text with unquoted spaces and
# parentheses that would break bash's own assignment syntax if sourced
# literally. Anything the caller or cron already exported wins, same
# precedence as every other .env-backed setting in this project.
#
# Expects $PYTHON to already be set and the working directory to already be
# the project root - both are true by the time either caller reaches this.

eval "$("$PYTHON" -c '
import os, shlex
import envfile
envfile.load_dotenv()
for k in ("CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ACCOUNT_ID", "WG_CF_PROJECT_NAME"):
    v = os.environ.get(k)
    if v:
        print(f"export {k}={shlex.quote(v)}")
')"

CF_PROJECT="${WG_CF_PROJECT_NAME:-waterguru-dashboard}"
