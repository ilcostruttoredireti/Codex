#!/usr/bin/env bash
# run.sh – convenience wrapper to start the sync inside a virtualenv
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${SCRIPT_DIR}/.venv"

# Create venv if needed
if [ ! -d "${VENV_DIR}" ]; then
  echo "Creating Python virtual environment…"
  python3 -m venv "${VENV_DIR}"
fi

# Activate
source "${VENV_DIR}/bin/activate"

# Install / upgrade dependencies
pip install -q --upgrade pip
pip install -q -r "${SCRIPT_DIR}/requirements.txt"

# Copy .env template if no .env exists
if [ ! -f "${SCRIPT_DIR}/.env" ]; then
  cp "${SCRIPT_DIR}/.env.example" "${SCRIPT_DIR}/.env"
  echo ""
  echo "⚠️  A new .env file was created from .env.example."
  echo "   Please fill in HUBSPOT_ACCESS_TOKEN and GMAIL_CREDENTIALS_PATH before running."
  echo ""
  exit 0
fi

# Pass through all arguments to main.py
cd "${SCRIPT_DIR}"
exec python main.py "$@"
