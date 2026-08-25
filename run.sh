#!/bin/bash
cd "$(dirname "$0")"

PY_CMD=""
if command -v python3 &>/dev/null; then
    PY_CMD="python3"
elif command -v python &>/dev/null; then
    PY_CMD="python"
fi

if [ -z "$PY_CMD" ]; then
    echo "[ERROR] Python was not found on your system."
    echo "Install Python 3.10+ using your package manager:"
    echo "  Ubuntu/Debian: sudo apt install python3 python3-venv python3-pip"
    echo "  Fedora:        sudo dnf install python3"
    echo "  macOS:         brew install python3"
    exit 1
fi

if [ ! -f ".venv/bin/python" ]; then
    $PY_CMD -m venv .venv >/dev/null 2>&1
fi

if [ ! -f ".venv/bin/python" ]; then
    echo "[ERROR] Failed to create virtual environment."
    echo "Make sure python3-venv is installed."
    exit 1
fi

.venv/bin/python -m pip install -r requirements.txt --quiet >/dev/null 2>&1
if [ $? -ne 0 ]; then
    .venv/bin/python -m pip install -r requirements.txt --quiet >/dev/null 2>&1
fi

mkdir -p data backups

.venv/bin/python main.py "$@"
