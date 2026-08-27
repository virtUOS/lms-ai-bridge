#!/usr/bin/env bash
# Index a Stud.IP course, then open the demo page against it.
#
#   ./demo-ui.sh [course_id]
#
# The page is scaffolding, not a product: the real front end is the LMS's. It
# exists so a human can watch the contract work — see bridge/demo_page.py.
#
# Prompts for your Stud.IP password. Nothing is stored or logged.
set -euo pipefail
cd "$(dirname "$0")"

COURSE="${1:-ad2ae056204adf6188ec3b8de140a9c5}"     # virtUOS-Weiterbildung
export STUDIP_URL="${STUDIP_URL:-https://studip.uni-osnabrueck.de}"

[ -f .env ] && set -a && . ./.env && set +a

PORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
STORE="$(mktemp -t bridge-ui-XXXXXX).json"
export BRIDGE_URL="http://127.0.0.1:${PORT}"

if [ -z "${STUDIP_USER:-}" ]; then read -r -p "Stud.IP username: " STUDIP_USER; fi
if [ -z "${STUDIP_PASSWORD:-}" ]; then read -r -s -p "Stud.IP password: " STUDIP_PASSWORD; echo; fi
export STUDIP_USER STUDIP_PASSWORD

cleanup() { kill "${SRV:-}" 2>/dev/null || true; rm -f "$STORE"; }
trap cleanup EXIT

echo
echo "▸ starting the bridge on port ${PORT}"
BRIDGE_PORT="$PORT" RETRIEVAL_STORE="$STORE" python3 -m bridge.server 2>&1 | sed 's/^/  /' &
SRV=$!
for _ in $(seq 1 60); do
  curl -sf "$BRIDGE_URL/v1/health" >/dev/null 2>&1 && break
  sleep 0.25
done

echo
echo "▸ indexing ${COURSE} — text now, any recordings in the background"
python3 adapters/studip_adapter.py index "$COURSE" | sed 's/^/  /'

URL="${BRIDGE_URL}/demo"
echo
echo "▸ open:  ${URL}"
echo "  course_ref for the form:  studip:${COURSE}"
echo
echo "  Ctrl-C to stop. The index is a temp file and is deleted on exit."
command -v open >/dev/null && open "$URL" || true

wait "$SRV"
