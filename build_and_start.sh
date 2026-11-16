#!/bin/sh

# Get the script's directory
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if ! sh build.sh; then
    echo "Failed to build the project"
    exit 1
fi

cd "$SCRIPT_DIR"

# Activate virtual environment (should already exist from build.sh)
if [ -d ".venv" ]; then
    . .venv/bin/activate
fi

echo ""
echo "Starting backend (including Remote MCP server if enabled)"
echo "  App URL:          http://127.0.0.1:8081"
echo "  MCP endpoint:     http://127.0.0.1:8081/mcp  (if REMOTE_MCP_SERVER_ENABLED=true)"
echo ""
open http://127.0.0.1:8081 2>/dev/null || xdg-open http://127.0.0.1:8081 2>/dev/null || true
python -m uvicorn app:app --port 8081 --reload
if [ $? -ne 0 ]; then
    echo "Failed to start backend"
    exit $?
fi
