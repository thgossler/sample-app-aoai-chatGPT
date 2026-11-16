#!/bin/sh

# Get the script's directory
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if ! sh build_backend.sh; then
    echo "Failed to build the project"
    exit 1
fi

if ! sh build_frontend.sh; then
    echo "Failed to build the project"
    exit 1
fi
