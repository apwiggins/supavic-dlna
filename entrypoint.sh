#!/usr/bin/env bash
set -e

echo "=========================================="
echo "Initializing SupaVic DLNA Server (Docker)"
echo "=========================================="

cd /app

# If running headlessly in Docker, prevent GUI/tray errors if applicable
export DISPLAY=""

# Locate the primary python execution file
if [ -f "main.py" ]; then
  PYTHON_EXEC="main.py"
elif [ -f "app.py" ]; then
  PYTHON_EXEC="app.py"
else
  # Fallback to search for python scripts in the directory
  PYTHON_EXEC=$(find . -maxdepth 1 -name "*.py" | head -n 1)
fi

if [ -z "$PYTHON_EXEC" ]; then
  echo "Error: Could not find a Python entry point script."
  exit 1
fi

echo "Starting application via: python $PYTHON_EXEC"
exec python "$PYTHON_EXEC" "$@"
