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
echo "Restoring backend python packages"
echo ""
if ! python -m pip install -r requirements.txt; then
    echo "Failed to restore backend python packages"
    exit 1
fi

if ! python -c 'from fastmcp import Client, FastMCP' 2>/dev/null; then
    echo "Repairing FastMCP installation"
    fastmcp_slim_version=$(python -c 'from importlib.metadata import version; print(version("fastmcp-slim"))') || exit 1
    python -m pip install --force-reinstall --no-deps "fastmcp-slim==$fastmcp_slim_version" || exit 1
    python -c 'from fastmcp import Client, FastMCP' || exit 1
fi
