#!/usr/bin/env bash
# One-time setup: Python venv + deps, checks for ffmpeg and Chrome.
set -e
cd "$(dirname "$0")"

echo "== Flow Music Kit setup =="

PY=""
for cand in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$cand" >/dev/null 2>&1; then
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
      PY="$cand"; break
    fi
  fi
done
if [ -z "$PY" ]; then
  echo "Python 3.10+ not found. macOS: brew install python@3.12" >&2
  exit 1
fi
echo "Python: $($PY --version)"

if [ ! -x venv/bin/python ]; then
  "$PY" -m venv venv
fi
venv/bin/pip install -q --upgrade pip
venv/bin/pip install -q -r requirements.txt -r requirements-dev.txt
echo "Dependencies installed."

if command -v ffmpeg >/dev/null && command -v ffprobe >/dev/null; then
  echo "ffmpeg: OK"
else
  echo "ffmpeg: MISSING — brew install ffmpeg (needed for durations + music videos)"
fi

if [ -d "/Applications/Google Chrome.app" ] || command -v google-chrome >/dev/null 2>&1; then
  echo "Chrome: OK"
else
  echo "Chrome: not found — the extension needs Chrome"
fi

mkdir -p output recipes

cat <<'MSG'

Next:
  1. ./run.sh                                  (agent on http://127.0.0.1:8300)
  2. chrome://extensions → Developer mode → Load unpacked → ./extension
  3. Open https://flowmusic.google and sign in; keep the tab open
  4. In Claude Code: /fm-capture generate_song   (one-time recording)
MSG
