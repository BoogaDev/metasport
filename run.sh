#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

python3.11 /Users/brandonalpert/Documents/Repositories/betsightpro_nhl/metasport/dk_odds.py \
  --selection today \
  --mode chunks \
  --teams "$SCRIPT_DIR/llmdocs/teams_rows.json" \
  --example "$SCRIPT_DIR/llmdocs/llmjsonoutput.json" \
  --css_zoom 2.0 \
  --chunk_height 700 \
  --scale 4 \
  --verbose

# python3.11 ./dk_odds.py \
# --selection today \
# --mode chunks \
# --teams "./llmdocs/teams_rows.json" \
# --example "./llmdocs/llmjsonoutput.json" \
# --css_zoom 2.0 \
# --chunk_height 700 \
# --scale 4 \
# --verbose
