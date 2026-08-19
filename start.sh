#!/bin/bash
# ─────────────────────────────────────────────
#  Scraper Dashboard — one-click start
#  Place this file inside scraper-dashboard-v2-main/
#  Then run:  bash start.sh
# ─────────────────────────────────────────────

# Go to the folder this script lives in
cd "$(dirname "$0")"

# ── Colours ──────────────────────────────────
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Colour

echo -e "${GREEN}Starting Scraper Dashboard...${NC}"

# ── Check dependencies ────────────────────────
if ! command -v python3 &>/dev/null; then
  echo -e "${RED}python3 not found. Please install Python 3.${NC}"
  exit 1
fi
if ! command -v npm &>/dev/null; then
  echo -e "${RED}npm not found. Please install Node.js.${NC}"
  exit 1
fi

# ── Install node deps if missing ──────────────
if [ ! -d "node_modules" ]; then
  echo -e "${YELLOW}node_modules not found — running npm install...${NC}"
  npm install
fi

# ── Install python deps if missing ───────────
if ! python3 -c "import flask, flask_cors, requests, bs4" &>/dev/null; then
  echo -e "${YELLOW}Installing Python dependencies...${NC}"
  pip install flask flask-cors requests beautifulsoup4 --break-system-packages -q
fi

# ── Start Python scraper server ───────────────
echo -e "${GREEN}[1/2] Starting Python scraper server on port 7001...${NC}"
python3 scraper-server/scraper_server.py &
PYTHON_PID=$!

# Give it a moment to boot
sleep 1

# ── Start Vite dev server ─────────────────────
echo -e "${GREEN}[2/2] Starting Vite dev server...${NC}"
npm run dev &
VITE_PID=$!

echo ""
echo -e "${GREEN}✓ Both servers are running.${NC}"
echo -e "  Dashboard  →  http://localhost:5173"
echo -e "  Scraper    →  http://localhost:7001"
echo ""
echo -e "${YELLOW}Press Ctrl+C to stop everything.${NC}"

# ── Shutdown handler ──────────────────────────
cleanup() {
  echo ""
  echo -e "${YELLOW}Stopping servers...${NC}"
  kill $PYTHON_PID 2>/dev/null
  kill $VITE_PID   2>/dev/null
  # Also kill any child processes they spawned
  pkill -P $PYTHON_PID 2>/dev/null
  pkill -P $VITE_PID   2>/dev/null
  echo -e "${GREEN}Done. Goodbye!${NC}"
  exit 0
}

trap cleanup SIGINT SIGTERM

# Keep script alive
wait
