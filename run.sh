#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

python3 dk_odds.py \
  --selection today \
  --mode chunks \
  --teams "$SCRIPT_DIR/llmdocs/teams_rows.json" \
  --example "$SCRIPT_DIR/llmdocs/llmjsonoutput.json" \
  --verbose

