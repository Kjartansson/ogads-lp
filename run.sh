#!/usr/bin/env bash
# Local dev server.
#
# --no-proxy-headers is deliberate and load-bearing. uvicorn defaults to
# proxy_headers=True, which REWRITES request.client.host from the caller's
# own X-Forwarded-For before any application code runs. That silently
# defeats the postback IP allowlist: anyone can claim to be an OGAds
# conversion server by setting a header. Turn it back on only when you are
# genuinely behind a proxy, and then pin the proxy:
#   --proxy-headers --forwarded-allow-ips=<cloudflare-ip>  (+ TRUSTED_PROXY_HOPS=1)
set -euo pipefail
cd "$(dirname "$0")"
[ -f .env ] || { echo "No .env -- copy .env.example to .env first."; exit 1; }
exec ./.venv/bin/uvicorn app.main:app \
  --host "${HOST:-127.0.0.1}" --port "${PORT:-8080}" \
  --no-proxy-headers --reload
