#!/bin/bash
# Double-click to open Rank Checker. The first run sets everything up.
cd "$(dirname "$0")" || exit 1

fail() {
  echo
  echo "$1"
  read -n 1 -s -r -p "Press any key to close this window."
  exit 1
}

if ! command -v python3 >/dev/null 2>&1 || ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' 2>/dev/null; then
  fail "Rank Checker needs Python 3.9 or newer. Install it from https://www.python.org/downloads/ and double-click this file again."
fi

if [ ! -f ".venv/.ready" ]; then
  echo "First run: setting up Rank Checker. This takes a minute or two and only happens once."
  rm -rf .venv
  python3 -m venv .venv || fail "Couldn't create Rank Checker's Python environment."
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -r requirements.txt || fail "Couldn't install Playwright. Check your internet connection and try again."
  .venv/bin/python -m playwright install chromium || fail "Couldn't download the browser Rank Checker uses. Check your internet connection and try again."
  touch .venv/.ready
  echo "Setup complete."
  echo
fi

.venv/bin/python app.py
