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

# Run ShareCV
python3 sharecv.py
