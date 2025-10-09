from __future__ import annotations

import os
import sys
import argparse
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
from .clients import create_supabase_client, create_apisports_client
from .db_repo import DBRepo
from .supabase_repo import SupabaseRepo
from .league_ids import discover_league_ids
from .constants import SPORT_META
from .ingest_schedule import ingest_schedule_for_sport
from .ingest_scores import ingest_scores_for_sport
from .ingest_odds import ingest_odds_for_sport
from .ingest_odds import ingest_odds_historical_for_sport


def _today_local_iso() -> str:
    return datetime.now().astimezone().date().isoformat()


def main(argv: list[str] | None = None) -> int:
    argv = argv or sys.argv[1:]
    parser = argparse.ArgumentParser(prog="metasport")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_sched = sub.add_parser("schedule")
    p_sched.add_argument("--date", default=_today_local_iso())
    p_sched.add_argument("--season", type=int, default=datetime.now().year)

    p_scores = sub.add_parser("scores")
    p_scores.add_argument("--season", type=int, default=datetime.now().year)
    p_scores.add_argument("--date", default=None, help="YYYY-MM-DD; if omitted, fetch live first then today")

    p_odds = sub.add_parser("odds")
    p_odds.add_argument("--date", default=_today_local_iso())
    p_odds.add_argument("--season", type=int, default=datetime.now().year)
    p_odds_hist = sub.add_parser("odds_historical")
    p_odds_hist.add_argument("--date", required=True)
    p_odds_hist.add_argument(
        "--snapshots",
        nargs="+",
        default=["09:00:00-05:00", "T-01:00"],
        help="Snapshot times (HH:MM:SS±TZ) and relative T- offsets",
    )

    p_odds_pg = sub.add_parser("odds_pregame")
    p_odds_pg.add_argument("--season", type=int, default=datetime.now().year)

    # debug_* commands removed for production cleanliness

    args = parser.parse_args(argv)

    # Load .env if present to simplify local runs
    load_dotenv()

    # Prefer direct DB connection if DATABASE_URL is present
    repo: SupabaseRepo | DBRepo
    if os.environ.get("DATABASE_URL"):
        repo = DBRepo()
    else:
        sb = create_supabase_client()
        repo = SupabaseRepo(sb)
    api = create_apisports_client()
    leagues = discover_league_ids(api)

    def _log(label: str, payload: dict) -> None:
        print(f"[{label.upper()}] {payload}")

    if args.cmd == "schedule":
        total = {}
        for sport in ("NHL", "MLB", "NFL"):
            res = ingest_schedule_for_sport(api, repo, sport, leagues[sport], args.season, args.date)
            total[sport] = res
        _log("schedule", total)
        return 0

    if args.cmd == "scores":
        total = {}
        for sport in ("NHL", "MLB", "NFL"):
            res = ingest_scores_for_sport(api, repo, sport, leagues[sport], args.season, args.date)
            total[sport] = res
        _log("scores", total)
        return 0

    if args.cmd == "odds":
        total = {}
        for sport in ("NHL", "MLB", "NFL"):
            res = ingest_odds_for_sport(api, repo, sport, leagues[sport], args.season, args.date)
            total[sport] = res
        _log("odds", total)
        return 0
    if args.cmd == "odds_historical":
        # Build snapshot ISO times
        date_ymd = args.date
        snaps: list[str] = []
        # Fixed clock times like 09:00:00-05:00
        for s in args.snapshots:
            if s.startswith("T-"):
                # relative one hour before commence will be handled per event in API (not supported directly),
                # so we approximate by requesting many snapshots isn't feasible; we'll include only fixed time here.
                continue
            snaps.append(f"{date_ymd}T{s}")
        total = {}
        for sport in ("NHL", "MLB", "NFL"):
            res = ingest_odds_historical_for_sport(api, repo, sport, date_ymd, snaps)
            total[sport] = res
        _log("odds_historical", total)
        return 0

    if args.cmd == "odds_pregame":
        # Find games starting within next 60 minutes and fetch odds per sport/date
        import pytz
        from datetime import timedelta
        now_utc = datetime.now(timezone.utc)
        in_60 = now_utc + timedelta(minutes=60)
        total = {}
        for sport in ("NHL", "MLB", "NFL"):
            # fetch games in window for this sport using DBRepo when available
            data = []
            start_iso = now_utc.isoformat()
            end_iso = in_60.isoformat()
            try:
                if hasattr(repo, "list_games_in_window"):
                    data = repo.list_games_in_window(sport, start_iso, end_iso)  # type: ignore[attr-defined]
                else:
                    q = (
                        repo.client.table("games")  # type: ignore[attr-defined]
                        .select("game_id,start_time_utc")
                        .eq("league", sport)
                        .gte("start_time_utc", start_iso)
                        .lte("start_time_utc", end_iso)
                    )
                    data = q.execute().data or []
            except Exception:
                data = []

            def _to_date_str(val):
                try:
                    # if datetime
                    return val.date().isoformat()
                except AttributeError:
                    # assume string
                    return str(val).split("T")[0]

            dates = sorted({_to_date_str(d["start_time_utc"]) for d in data if d and d.get("start_time_utc")})
            sport_total = {"events": 0, "inserted": 0}
            for ds in dates:
                res = ingest_odds_for_sport(api, repo, sport, leagues[sport], args.season, ds)
                sport_total["events"] += res.get("events", 0)
                sport_total["inserted"] += res.get("inserted", 0)
            total[sport] = sport_total
        _log("odds_pregame", total)
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())


