#!/usr/bin/env bash
# End-to-end demo against a real Stud.IP course.
#
#   ./demo-studip.sh [course_id]
#
# Prompts for your Stud.IP password. Nothing is stored or logged.
# Default course: High Performance Computing on studip-test.
set -euo pipefail
cd "$(dirname "$0")"

COURSE="${1:-e20f2c94735335abba3bea70a8ebc311}"
export STUDIP_URL="${STUDIP_URL:-https://studip-test.uni-osnabrueck.de}"
PORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
STORE="$(mktemp -t bridge-studip-XXXXXX).json"
export BRIDGE_URL="http://127.0.0.1:${PORT}"

if [ -z "${STUDIP_USER:-}" ]; then read -r -p "Stud.IP username: " STUDIP_USER; fi
if [ -z "${STUDIP_PASSWORD:-}" ]; then read -r -s -p "Stud.IP password: " STUDIP_PASSWORD; echo; fi
export STUDIP_USER STUDIP_PASSWORD

cleanup() { kill "${SRV:-}" 2>/dev/null || true; rm -f "$STORE"; }
trap cleanup EXIT

echo
echo "▸ starting bridge (port ${PORT})"
BRIDGE_PORT="$PORT" RETRIEVAL_STORE="$STORE" python3 -m bridge.server 2>&1 | sed 's/^/  /' &
SRV=$!
for _ in $(seq 1 40); do curl -sf "$BRIDGE_URL/v1/health" >/dev/null 2>&1 && break; sleep 0.2; done

echo
echo "▸ pulling course content from Stud.IP and indexing it"
python3 adapters/studip_adapter.py index "$COURSE" | sed 's/^/  /'

for q in \
  "Wie reiche ich einen Job über SLURM ein?" \
  "Wie bekomme ich Zugang zum Cluster?" \
  "Wie benutze ich conda auf dem HPC?" \
  "Wie hoch ist die Studiengebühr?"
do
  echo
  echo "▸ Frage: $q"
  python3 adapters/studip_adapter.py ask "$COURSE" "$q" | sed 's/^/  /'
done

echo
echo "▸ The last question has no answer in this course — retrieval returns"
echo "  nothing rather than letting the model invent something."
