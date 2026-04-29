#!/bin/bash
# ─────────────────────────────────────────────────────────────────────────────
# start.sh — one command to start the Polymarket app
#
# Run it from the project folder:
#   bash start.sh
#
# It will:
#   1. Create a Python virtual environment if one doesn't exist
#   2. Install all required packages
#   3. Create any missing folders
#   4. Start the app at http://localhost:8000
#
# Leave this Terminal window running. Open your browser to http://localhost:8000
# ─────────────────────────────────────────────────────────────────────────────

set -e
cd "$(dirname "$0")"

echo ""
echo "  Polymarket News-Reaction — startup"
echo "  ───────────────────────────────────"

# 1. Virtual environment
if [ ! -f ".venv/bin/python" ]; then
  echo "  → First run: creating Python environment (takes ~30 seconds)..."
  python3 -m venv .venv
fi

# 2. Dependencies
echo "  → Checking packages..."
.venv/bin/pip install -r requirements.txt -q --disable-pip-version-check

# 3. Required directories
mkdir -p app/static

# 4. .env reminder
if [ ! -f ".env" ]; then
  echo ""
  echo "  ⚠️  No .env file found. Copying from .env.example..."
  cp .env.example .env
  echo "  Open .env and add your OPENAI_API_KEY for full functionality."
  echo ""
fi

echo ""
echo "  ✅ Ready. Open http://localhost:8000 in your browser."
echo "  Press Ctrl+C in this window to stop."
echo ""

.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
