from __future__ import annotations

import os
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .clients import create_supabase_client, create_apisports_client
from .supabase_repo import SupabaseRepo
from .db_repo import DBRepo
from .league_ids import discover_league_ids
from .ingest_schedule import ingest_schedule_for_sport
from .ingest_scores import ingest_scores_for_sport
from .ingest_odds import ingest_odds_for_sport
from .config import TIMEZONE


def now_et() -> datetime:
    return datetime.now(ZoneInfo(TIMEZONE))


def today_et_iso() -> str:
    return now_et().date().isoformat()


def safe_call(label: str, fn, *args, **kwargs) -> dict:
    try:
        res = fn(*args, **kwargs) or {}
        print(f"[{label}] {res}")
        return res
    except Exception as exc:
        print(f"[{label}] error: {exc}")
        return {"error": str(exc)}


def main_loop() -> None:
    # Repos / clients
    # Prefer direct DB connection if available
    if os.environ.get("DATABASE_URL"):
        repo = DBRepo()
    else:
        sb = create_supabase_client()
        repo = SupabaseRepo(sb)
    api = create_apisports_client()
    leagues = discover_league_ids(api)

    # Cadence timers
    next_schedule_at = datetime.now(timezone.utc)
    next_scores_at = datetime.now(timezone.utc)
    next_pregame_at = datetime.now(timezone.utc)
    last_odds_am_for_date: str | None = None

    print("[WORKER] started. Cadences: schedule=2h, scores=2m, pregame=5m, odds-am=09:00 ET")

    while True:
        utc_now = datetime.now(timezone.utc)
        et_now = now_et()
        et_today = et_now.date().isoformat()
        season_year = et_now.year

        # 1) Odds AM once per day at/after 09:00 ET
        if last_odds_am_for_date != et_today and et_now.hour >= 9:
            for sport in ("NHL", "MLB", "NFL"):
                safe_call(
                    f"ODDS_AM {sport}",
                    ingest_odds_for_sport,
                    api,
                    repo,
                    sport,
                    leagues[sport],
                    season_year,
                    et_today,
                )
            last_odds_am_for_date = et_today

        # 2) Schedule every 2 hours
        if utc_now >= next_schedule_at:
            for sport in ("NHL", "MLB", "NFL"):
                safe_call(
                    f"SCHEDULE {sport}",
                    ingest_schedule_for_sport,
                    api,
                    repo,
                    sport,
                    leagues[sport],
                    season_year,
                    et_today,
                )
            next_schedule_at = utc_now + timedelta(hours=2)

        # 3) Scores every 2 minutes (date-bounded to today)
        if utc_now >= next_scores_at:
            for sport in ("NHL", "MLB", "NFL"):
                safe_call(
                    f"SCORES {sport}",
                    ingest_scores_for_sport,
                    api,
                    repo,
                    sport,
                    leagues[sport],
                    season_year,
                    et_today,
                )
            next_scores_at = utc_now + timedelta(minutes=2)

        # 4) Pregame odds every 5 minutes
        if utc_now >= next_pregame_at:
            for sport in ("NHL", "MLB", "NFL"):
                safe_call(
                    f"ODDS_PREGAME {sport}",
                    ingest_odds_for_sport,
                    api,
                    repo,
                    sport,
                    leagues[sport],
                    season_year,
                    et_today,
                )
            next_pregame_at = utc_now + timedelta(minutes=5)

        # Sleep until the next nearest tick
        next_tick = min(next_schedule_at, next_scores_at, next_pregame_at)
        sleep_s = max(1.0, (next_tick - datetime.now(timezone.utc)).total_seconds())
        time.sleep(min(sleep_s, 60.0))  # cap sleep so we react to AM odds boundary


if __name__ == "__main__":
    main_loop()


