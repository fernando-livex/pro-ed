#!/usr/bin/env bash
# Run the proxy locally for this-week testing. Pair with a Cloudflare quick tunnel:
#   Terminal 1:  bash run-local.sh
#   Terminal 2:  cloudflared tunnel --url http://localhost:8080
# Then give the printed https://<...>.trycloudflare.com URL + your PROXY_API_KEY to the SE.
set -euo pipefail
cd "$(dirname "$0")"

if [ ! -f .env ]; then
  echo "No .env found. Run:  cp .env.example .env  then fill in NAVIGA_CLIENT_SECRET + PROXY_API_KEY" >&2
  exit 1
fi
set -a; . ./.env; set +a
: "${NAVIGA_CLIENT_ID:?}"; : "${NAVIGA_CLIENT_SECRET:?}"; : "${PROXY_API_KEY:?}"

python3 -m venv .venv 2>/dev/null || true
# shellcheck disable=SC1091
source .venv/bin/activate 2>/dev/null || true
python3 -m pip install -q -r requirements.txt

echo "Proxy up on http://localhost:${PORT:-8080}"
echo "  POST /order-tracking   {\"ponumber\":\"10446576\"}  (header X-Api-Key: \$PROXY_API_KEY)"
echo "  POST /invoice-tracking {\"customerid\":\"...\",\"invoiceid\":\"...\"}"
python3 main.py
