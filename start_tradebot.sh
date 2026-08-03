#!/bin/bash
echo ""
echo " ======================================="
echo "   TradeBot - Starting..."
echo " ======================================="
echo ""
cd "$(dirname "$0")"
if [ -x .venv/bin/python ]; then
    PYTHON=.venv/bin/python
    PIP=.venv/bin/pip
elif command -v python3 &> /dev/null; then
    PYTHON=python3
    PIP=pip3
else
    echo "ERROR: Python 3 not found. Install from https://python.org"
    exit 1
fi
echo "Checking dependencies..."
$PIP install -r requirements.txt -q 2>/dev/null || $PIP install -r requirements.txt -q --break-system-packages
echo ""
echo " Open your browser to: http://localhost:5000"
echo " Press Ctrl+C to stop"
echo ""
$PYTHON app.py
