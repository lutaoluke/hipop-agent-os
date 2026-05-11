#!/bin/bash
# hipop-agent-os smoke test runner
set -e
cd "$(dirname "$0")/.."
URL="${HIPOP_URL:-http://localhost:8765}"
echo "→ smoke test 目标 URL: $URL"
if ! curl -sS -m 5 "$URL/health" > /dev/null; then
  echo "✗ server 不可达 ($URL)"; exit 2
fi
ready=$(curl -sS -m 5 "$URL/ready")
mode=$(echo "$ready" | python3 -c "import sys,json; print(json.load(sys.stdin).get('mode','?'))" 2>/dev/null || echo "?")
echo "→ DB mode: $mode"
python3 tests/smoke_chat.py --url "$URL" "$@"
