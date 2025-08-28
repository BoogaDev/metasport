Metasport Ingestion Service

Overview

Ingests schedules, scores, and odds for NHL, MLB, and NFL from API-Sports and upserts into Supabase. Exposes a health endpoint via Flask on port 8000.

Environment

Copy .env.example to .env and fill values.

APISPORTS_KEY=...
SUPABASE_URL=...
SUPABASE_SERVICE_ROLE=...
SUPABASE_SCHEMA=public
TZ=America/New_York

Install

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

Run health server

python app.py

CLI examples

# Update today's schedule for all sports
python -m src.cli schedule --date today

# Update live scores
python -m src.cli scores

# Update odds for today
python -m src.cli odds --date today

Deploy

- Build with Dockerfile and deploy to DigitalOcean App Platform or Droplet.
- Use do-app-spec.yaml for scheduled jobs (cron) and a web service on port 8000.

