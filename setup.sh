#!/usr/bin/env bash
# One-time setup. Safe to re-run.
set -e
cd "$(dirname "$0")"

python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt
echo "light venv ready"

# The TTS/ASR stack is ~3GB. If REEL_VENV in .env already points at an existing
# install (the AI_News pipeline's, say), reuse it rather than building another.
if grep -qs '^REEL_VENV=/' .env; then
  echo "REEL_VENV points at an existing install — skipping the 3GB stack"
else
  python3 -m venv .venv-reel
  .venv-reel/bin/pip install -q --upgrade pip
  .venv-reel/bin/pip install -q -r requirements-reel.txt
  echo "worker venv ready"
fi

[ -f .env ] || cp .env.example .env
./reelforge.py probe
