#!/usr/bin/env bash
# start.sh — Start the Knight Novel Scraper on Linux / Ubuntu
# Usage: ./start.sh
# Both servers run in the background and are killed cleanly on Ctrl+C.

set -e
ROOT="$(cd "$(dirname "$0")" && pwd)"
SERVER="$ROOT/scraper-server"

echo ""
echo "═══════════════════════════════════════════════"
echo "  Knight Novel Scraper Dashboard"
echo "  Starting Python server + Vite dashboard..."
echo "═══════════════════════════════════════════════"
echo ""

# ── Python scraper server ────────────────────────────────────────────────────
echo "► Starting Python scraper server on :7832 ..."
cd "$SERVER"
python3 scraper_server.py &
PY_PID=$!
echo "  Python PID: $PY_PID"

# ── Wait for the Python server to be ready ───────────────────────────────────
echo "  Waiting for server to be ready..."
for i in $(seq 1 20); do
  if curl -s http://localhost:7832/health > /dev/null 2>&1; then
    echo "  ✓ Python server is up!"
    break
  fi
  sleep 0.5
done

# ── Vite dashboard ───────────────────────────────────────────────────────────
echo ""
echo "► Starting Vite dashboard on :5174 ..."
cd "$ROOT"
npm run dev &
VITE_PID=$!
echo "  Vite PID: $VITE_PID"

echo ""
echo "═══════════════════════════════════════════════"
echo "  Dashboard:  http://$(hostname -I | awk '{print $1}'):5174"
echo "  API Server: http://$(hostname -I | awk '{print $1}'):7832"
echo ""
echo "  Press Ctrl+C to stop both servers."
echo "═══════════════════════════════════════════════"
echo ""

# ── Graceful shutdown ────────────────────────────────────────────────────────
cleanup() {
  echo ""
  echo "Stopping servers..."
  kill "$PY_PID"   2>/dev/null || true
  kill "$VITE_PID" 2>/dev/null || true
  echo "Done."
}
trap cleanup INT TERM

# Wait for either process to exit
wait -n "$PY_PID" "$VITE_PID" 2>/dev/null || wait
