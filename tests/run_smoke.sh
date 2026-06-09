#!/bin/bash
# hipop-agent-os smoke test runner
set -e
cd "$(dirname "$0")/.."
PYTHON_BIN="${PYTHON:-/Library/Developer/CommandLineTools/Library/Frameworks/Python3.framework/Versions/3.9/Resources/Python.app/Contents/MacOS/Python}"
if [ ! -x "$PYTHON_BIN" ]; then
  PYTHON_BIN="python3"
fi

if [ -n "${HIPOP_ENV_FILE:-}" ] && [ -f "$HIPOP_ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$HIPOP_ENV_FILE"
  set +a
elif [ -f ".env.local" ]; then
  set -a
  # shellcheck disable=SC1091
  source ".env.local"
  set +a
elif _GIT_COMMON="$(git rev-parse --git-common-dir 2>/dev/null)" && \
     [ -f "${_GIT_COMMON%/.git}/.env.local" ]; then
  # In a git worktree the common .git dir points to the main checkout —
  # fall back to that repo's .env.local so DB_URL/JWT_SECRET are available.
  set -a
  # shellcheck disable=SC1090
  source "${_GIT_COMMON%/.git}/.env.local"
  set +a
fi

URL="${HIPOP_URL:-http://localhost:8765}"
echo "→ smoke test 目标 URL: $URL"
if ! curl -sS -m 5 "$URL/health" > /dev/null; then
  echo "✗ server 不可达 ($URL)"; exit 2
fi
ready=$(curl -sS -m 5 "$URL/ready")
mode=$(echo "$ready" | "$PYTHON_BIN" -c "import sys,json; print(json.load(sys.stdin).get('mode','?'))" 2>/dev/null || echo "?")
echo "→ DB mode: $mode"

if [ -z "${HIPOP_AUTH_TOKEN:-}" ] && [ -n "${DB_URL:-}" ] && [ -n "${JWT_SECRET:-}" ]; then
  HIPOP_AUTH_TOKEN=$(PYTHONPATH="$PWD" "$PYTHON_BIN" - <<'PY'
from hipop.server import auth, data

rows = data._fetch(
    "SELECT id, tenant_id FROM users WHERE active=1 AND tenant_id=1 ORDER BY id LIMIT 1"
)
if not rows:
    rows = data._fetch("SELECT id, tenant_id FROM users WHERE active=1 ORDER BY id LIMIT 1")
if rows:
    row = rows[0]
    print(auth.make_jwt(int(row["id"]), int(row["tenant_id"])))
PY
)
  export HIPOP_AUTH_TOKEN
  if [ -n "$HIPOP_AUTH_TOKEN" ]; then
    echo "→ auth: generated smoke Bearer token from DB_URL/JWT_SECRET"
  fi
fi

PYTHONPATH="$PWD${PYTHONPATH:+:$PYTHONPATH}" "$PYTHON_BIN" tests/smoke_chat.py --url "$URL" "$@"
