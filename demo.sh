#!/usr/bin/env bash
# One-command demo. Needs nothing but Python 3.11+.
#
#   ./demo.sh
#
# Starts the bridge, indexes a fixture course, asks three questions, stops.
# Works offline; set OPENAI_BASE_URL and MODEL for real model answers.
set -euo pipefail
cd "$(dirname "$0")"

# Pick a free port so the demo never collides with something already running.
PORT="${BRIDGE_PORT:-$(python3 -c "import socket;s=socket.socket();s.bind((\"127.0.0.1\",0));print(s.getsockname()[1]);s.close()")}"
B="http://127.0.0.1:${PORT}"
STORE="$(mktemp -t bridge-demo-XXXXXX).json"

cleanup() { kill "${SRV:-}" 2>/dev/null || true; rm -f "$STORE"; }
trap cleanup EXIT

echo "▸ starting bridge on port ${PORT}"
BRIDGE_PORT="$PORT" RETRIEVAL_STORE="$STORE" python3 -m bridge.server 2>&1 | sed 's/^/  /' &
SRV=$!
ready=0
for _ in $(seq 1 40); do
  if curl -sf "$B/v1/health" >/dev/null 2>&1; then ready=1; break; fi
  sleep 0.2
done
if [ "$ready" -ne 1 ]; then
  echo "  bridge failed to start — see the log above" >&2
  exit 1
fi

echo
echo "▸ capabilities"
curl -s "$B/v1/capabilities" | python3 -m json.tool | sed 's/^/  /'

echo
echo "▸ indexing fixture course (as an LMS adapter would)"
curl -s -X POST "$B/v1/index" -H 'Content-Type: application/json' \
  --data-binary @fixtures/demo-course.json | python3 -m json.tool | sed 's/^/  /'

ask() {
  echo
  echo "▸ Frage: $1"
  curl -s -X POST "$B/v1/chat" -H 'Content-Type: application/json' \
    -d "{\"course_ref\":\"demo:fp-vorlesung\",\"messages\":[{\"role\":\"user\",\"content\":\"$1\"}]}" \
  | python3 fixtures/_format_answer.py
}

ask "Was sind die zentralen Operationen eines Monads?"
ask "Bis wann muss das zweite Übungsblatt abgegeben werden?"
ask "Wie funktioniert Quantenverschränkung?"

echo
echo "▸ done. The third question shows what happens when the course material"
echo "  does not contain the answer — retrieval returns nothing rather than"
echo "  the model inventing something."
