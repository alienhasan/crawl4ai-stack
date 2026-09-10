#!/usr/bin/env bash
# Start the crawl4ai server (FastAPI + uvicorn) from the rebuilt venv.
#
#   bash run.sh            start in the foreground
#   bash run.sh --check    only report whether the server is already up
#
# Env overrides: PROJECT_DIR, VENV, PORT (health-check port only)
set -euo pipefail

PROJECT_DIR="${PROJECT_DIR:-$HOME/hermesworkspace/crawl4ai-project}"
VENV="${VENV:-$PROJECT_DIR/.crawl4ai-venv}"
PORT="${PORT:-8000}"
URL="http://localhost:${PORT}/docs"

health() { curl -s -o /dev/null -w '%{http_code}' --max-time 5 "$URL" 2>/dev/null || true; }

CODE="$(health)"
if [ "${1:-}" = "--check" ]; then
  if [ "$CODE" = "200" ]; then echo "server UP   ($URL -> 200)"; else echo "server DOWN ($URL -> ${CODE:-000})"; fi
  exit 0
fi
if [ "$CODE" = "200" ]; then
  echo "server already running on port $PORT ($URL -> 200)"
  exit 0
fi

[ -x "$VENV/bin/python" ] || { echo "no venv at $VENV - run: bash setup.sh" >&2; exit 1; }
[ -f "$PROJECT_DIR/crawl_api.py" ] || { echo "crawl_api.py not found in $PROJECT_DIR - run: bash setup.sh" >&2; exit 1; }

# crawl_api.py writes to the cwd-relative logs/ dir, so cwd must be the project.
cd "$PROJECT_DIR"
echo "starting crawl4ai server on http://0.0.0.0:$PORT  (Ctrl-C to stop)"
exec "$VENV/bin/python" crawl_api.py
