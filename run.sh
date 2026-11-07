#!/usr/bin/env bash
# Start the Flow Music Kit agent (API :8300, extension WebSocket :9300).
set -e
cd "$(dirname "$0")"
if [ ! -x venv/bin/python ]; then
  echo "venv missing — run ./setup.sh first" >&2
  exit 1
fi
exec venv/bin/python -m agent.main
