#!/bin/sh

# Get the script's directory
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Create virtual environment if it doesn't exist
if [ ! -d ".venv" ]; then
    echo ""
    echo "Creating virtual environment..."
    echo ""
    python3 -m venv .venv
    if [ $? -ne 0 ]; then
        echo "Failed to create virtual environment"
        exit $?
    fi
fi

# Activate virtual environment
. .venv/bin/activate

echo ""
echo "Starting backend (including Remote MCP server if enabled)"
echo "  App URL:          http://127.0.0.1:8081"
echo "  MCP endpoint:     http://127.0.0.1:8081/mcp  (if REMOTE_MCP_SERVER_ENABLED=true)"
echo ""
dotenv
open http://127.0.0.1:8081 2>/dev/null || xdg-open http://127.0.0.1:8081 2>/dev/null || true
python -m uvicorn app:app --port 8081 --reload
if [ $? -ne 0 ]; then
    echo "Failed to start backend"
    exit $?
fi
