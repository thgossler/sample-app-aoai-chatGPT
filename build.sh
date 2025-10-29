#!/bin/sh

# Get the script's directory
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

sh build_backend.sh
if [ $? -ne 0 ]; then
    echo "Failed to build the project"
    exit $?
fi

sh build_frontend.sh
if [ $? -ne 0 ]; then
    echo "Failed to build the project"
    exit $?
fi
