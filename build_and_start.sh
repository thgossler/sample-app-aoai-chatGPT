#!/bin/sh

# Get the script's directory
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

sh build.sh
if [ $? -ne 0 ]; then
    echo "Failed to build the project"
    exit $?
fi

cd "$SCRIPT_DIR"

# Activate virtual environment (should already exist from build.sh)
if [ -d ".venv" ]; then
    . .venv/bin/activate
fi

echo ""
echo "Starting backend"
echo ""
dotenv
open http://127.0.0.1:8081 2>/dev/null || xdg-open http://127.0.0.1:8081 2>/dev/null || true
python -m uvicorn app:app --port 8081 --reload
if [ $? -ne 0 ]; then
    echo "Failed to start backend"
    exit $?
fi
