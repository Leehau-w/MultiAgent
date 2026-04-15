#!/usr/bin/env bash
set -e

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
BACKEND_DIR="$ROOT_DIR/backend"
FRONTEND_DIR="$ROOT_DIR/frontend"

# Colors
GREEN='\033[0;32m'
CYAN='\033[0;36m'
NC='\033[0m'

echo -e "${CYAN}[MultiAgent Studio]${NC} Starting services..."

# --- Backend ---
echo -e "${GREEN}[Backend]${NC} Installing Python dependencies..."
cd "$BACKEND_DIR"
if [ ! -d ".venv" ]; then
    python -m venv .venv
fi
source .venv/bin/activate 2>/dev/null || source .venv/Scripts/activate 2>/dev/null
pip install -q -r requirements.txt

echo -e "${GREEN}[Backend]${NC} Starting FastAPI on :8000..."
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload &
BACKEND_PID=$!

# --- Frontend ---
echo -e "${GREEN}[Frontend]${NC} Installing Node dependencies..."
cd "$FRONTEND_DIR"
npm install --silent

echo -e "${GREEN}[Frontend]${NC} Starting Vite dev server on :5173..."
npm run dev &
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
echo -e "${CYAN}  Frontend: http://localhost:5173${NC}"
echo -e "${CYAN}  Backend:  http://localhost:8000${NC}"
echo -e "${CYAN}  API Docs: http://localhost:8000/docs${NC}"
echo -e "${CYAN}============================================${NC}"
echo ""

wait
