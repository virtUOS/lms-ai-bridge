#!/usr/bin/env bash
# End-to-end demo against a real Moodle course.
#
#   ./demo-moodle.sh [course_id]
#
# Needs MOODLE_URL and MOODLE_TOKEN (a web-service token; see README).
# Reads them from .env if present.
set -euo pipefail
cd "$(dirname "$0")"

[ -f .env ] && { set -a; . ./.env; set +a; }

COURSE="${1:-5}"
: "${MOODLE_URL:?set MOODLE_URL (e.g. https://moodle.example.org)}"
: "${MOODLE_TOKEN:?set MOODLE_TOKEN — Site administration → Server → Web services → Manage tokens}"
export MOODLE_URL MOODLE_TOKEN

PORT="$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()')"
STORE="$(mktemp -t bridge-moodle-XXXXXX).json"
export BRIDGE_URL="http://127.0.0.1:${PORT}"

cleanup() { kill "${SRV:-}" 2>/dev/null || true; rm -f "$STORE"; }
trap cleanup EXIT

echo
echo "▸ starting bridge (port ${PORT})"
BRIDGE_PORT="$PORT" RETRIEVAL_STORE="$STORE" python3 -m bridge.server 2>&1 | sed 's/^/  /' &
SRV=$!
for _ in $(seq 1 40); do curl -sf "$BRIDGE_URL/v1/health" >/dev/null 2>&1 && break; sleep 0.2; done

echo
echo "▸ pulling course content from Moodle and indexing it"
echo "  (course description, module text, and the contents of every file)"
python3 adapters/moodle_adapter.py index "$COURSE" | sed 's/^/  /'

for q in \
  "Worum geht es in diesem Kurs?" \
  "Was empfiehlt der Bericht zum Einsatz von KI an Hochschulen?" \
  "Welche Faktoren beeinflussen laut den Studien den Erfolg von KI in der Lehre?" \
  "Wie hoch ist die Studiengebühr?"
do
  echo
  echo "▸ Frage: $q"
  python3 adapters/moodle_adapter.py ask "$COURSE" "$q" | sed 's/^/  /'
done

echo
echo "▸ The last question has no answer in this course — retrieval returns"
echo "  nothing rather than letting the model invent something."
