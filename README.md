DK Odds App

Overview
- Standalone script to capture DraftKings NHL screenshots, extract odds with OpenAI, and insert into your database tables (`market_selections`, `odds`).
- Self-contained; move this folder anywhere. Uses `.env` for credentials.

Prerequisites
- Python 3.11+
- A valid OpenAI API key (and org/project if using a project key)
- Either a Postgres `DATABASE_URL` or Supabase `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE`

Install
```bash
python3 -m venv .venv && source .venv/bin/activate
python3 -m pip install -r requirements.txt
# Playwright browsers
python3 -m playwright install chromium
```

Configure
1) Copy `.env.example` to `.env` and fill values.
2) Optionally replace `teams_rows.json` with your teams table export.

Run
```bash
source .venv/bin/activate
python3 dk_odds.py \
  --selection today \
  --mode chunks \
  --teams ./llmdocs/teams_rows.json \
  --example ./llmdocs/llmjsonoutput.json \
  --verbose
```

Notes
- Screenshots go to `./artifacts/` and a `llmjsonoutput_*.json` is written next to the script for audit.
- The script accepts several spread/odds JSON shapes and rounds start times to the nearest :00/:30.
- `game_id` slugs are built as AWAY_HOME_YYYY-MM-DD, using the payload time zone date (default ET) to match DB.


