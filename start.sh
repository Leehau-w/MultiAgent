#!/usr/bin/env bash
set -e

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
FRONTEND_DIR="$ROOT_DIR/frontend"

# ---- Usage: ./start.sh [project_path] [backend_port] [frontend_port] ----
export MULTIAGENT_PROJECT="${1:-$MULTIAGENT_PROJECT}"
BACKEND_PORT="${2:-${BACKEND_PORT:-8000}}"
FRONTEND_PORT="${3:-${FRONTEND_PORT:-5173}}"
export BACKEND_PORT

# Colors
GREEN='\033[0;32m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${CYAN}[MultiAgent Studio]${NC} Starting services..."
[ -n "$MULTIAGENT_PROJECT" ] && echo -e "${CYAN}  Project: $MULTIAGENT_PROJECT${NC}"

# --- Backend ---
echo -e "${GREEN}[Backend]${NC} Installing Python dependencies..."
cd "$BACKEND_DIR"
if [ ! -d ".venv" ]; then
    python -m venv .venv
fi
source .venv/bin/activate 2>/dev/null || source .venv/Scripts/activate 2>/dev/null
pip install -q -r requirements.txt

echo -e "${GREEN}[Backend]${NC} Starting FastAPI on :$BACKEND_PORT..."
uvicorn app.main:app --host 0.0.0.0 --port "$BACKEND_PORT" --reload &
BACKEND_PID=$!

# --- Frontend ---
echo -e "${GREEN}[Frontend]${NC} Installing Node dependencies..."
cd "$FRONTEND_DIR"
npm install --silent

echo -e "${GREEN}[Frontend]${NC} Starting Vite dev server on :$FRONTEND_PORT..."
npx vite --port "$FRONTEND_PORT" &
FRONTEND_PID=$!

# Trap to kill both on exit
cleanup() {
    echo ""
    echo -e "${CYAN}[MultiAgent Studio]${NC} Shutting down..."
    kill $BACKEND_PID 2>/dev/null
    kill $FRONTEND_PID 2>/dev/null
    exit 0
}
trap cleanup SIGINT SIGTERM

echo ""
echo -e "${CYAN}============================================${NC}"
echo -e "${CYAN}  MultiAgent Studio is running!${NC}"
echo -e "${CYAN}  Frontend: http://localhost:$FRONTEND_PORT${NC}"
echo -e "${CYAN}  Backend:  http://localhost:$BACKEND_PORT${NC}"
echo -e "${CYAN}  API Docs: http://localhost:$BACKEND_PORT/docs${NC}"
[ -n "$MULTIAGENT_PROJECT" ] && echo -e "${CYAN}  Project:  $MULTIAGENT_PROJECT${NC}"
echo -e "${CYAN}============================================${NC}"
echo ""

wait
