#!/bin/bash

# Get the directory where the script is located
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
cd "$DIR"

echo "========================================"
echo "  ShareCV Quick Start (macOS/Linux)"
echo "========================================"
echo ""

# Check for Python 3
if ! command -v python3 &> /dev/null
then
    echo "[ERROR] python3 could not be found. Please install Python 3."
    exit 1
fi

# Auto-install dependencies if anything is missing (also covers requirement changes)
DEPS_CHECK="import websockets, fastapi, httpx, uvicorn, pyperclip"
if [ "$(uname)" = "Darwin" ]; then
    DEPS_CHECK="$DEPS_CHECK, AppKit"
fi
if ! python3 -c "$DEPS_CHECK" >/dev/null 2>&1; then
    echo "[*] Installing dependencies (this only runs when something is missing)..."
    python3 -m pip install -r requirements.txt
    echo ""
fi

# Run ShareCV
python3 sharecv.py "$@"
