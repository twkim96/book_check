#!/bin/zsh
set -e

SCRIPT_DIR="${0:A:h}"
cd "$SCRIPT_DIR"

export PYTHONDONTWRITEBYTECODE=1
python3 folderling.py

echo
echo "Done. Press any key to close."
read -k 1
