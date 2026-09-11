#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# Resolve dk_odds.py relative to this script so run.sh works from any
# checkout location (the repo lives under different $HOME paths per machine).
python3.11 "$SCRIPT_DIR/dk_odds.py" \
  --selection today \
  --mode chunks \
  --teams "$SCRIPT_DIR/llmdocs/teams_rows.json" \
  --example "$SCRIPT_DIR/llmdocs/llmjsonoutput.json" \
  --css_zoom 2.0 \
  --chunk_height 700 \
  --scale 4 \
  --verbose
