#!/bin/bash
set -e

echo "=== GramSave ==="

# Check Python
if ! command -v python3 &>/dev/null; then
  echo "Error: python3 not found. Install Python 3.10+ from https://python.org"
  exit 1
fi

PYTHON_VERSION=$(python3 -c "import sys; print(sys.version_info.minor)")
if [ "$PYTHON_VERSION" -lt 10 ]; then
  echo "Error: Python 3.10+ required"
  exit 1
fi

# Install dependencies
echo "Installing dependencies..."
python3 -m pip install -q -r requirements.txt

# Launch
echo "Starting server at http://localhost:5055"
echo "Press Ctrl+C to stop."
echo ""
python3 app.py
