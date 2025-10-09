from __future__ import annotations

from typing import Dict, Any
from datetime import datetime
from zoneinfo import ZoneInfo
import httpx

from .constants import SPORT_META
from .config import TIMEZONE
from .util import parse_datetime_to_utc, build_game_slug, normalize_team_name, derive_team_name_only, slugify_team_name_only
from .supabase_repo import SupabaseRepo  # for type compatibility
from .db_repo import DBRepo


def _resolve_team(repo: SupabaseRepo | DBRepo, sport: str, abbreviation: str, name: str, league: str, api_league_number: int | None = None, api_team_number: int | None = None, api_team_name: str | None = None) -> str:
    # Do not create teams; resolve strictly from DB using normalized matching rules.
    # 1) Try sport+abbr (CI)
    team = repo.get_team_by_sport_abbr(sport, abbreviation)  # type: ignore[arg-type]
    if team:
        return team["id"]
    # 2) Compare last word of the name to team_name_only (CI) within sport
    lname = (api_team_name or name or "").strip().split(" ")[-1].upper()
    cand = []
    try:
        cand = repo.list_teams_by_team_name_only_ci(sport, lname)  # type: ignore[attr-defined]
    except Exception:
        cand = []
    if len(cand) == 1:
        return cand[0]["id"]
    if len(cand) > 1:
        # 3) Disambiguate by full name within sport (CI)
        try:
            team2 = repo.get_team_by_sport_name_ci(sport, name)  # type: ignore[attr-defined]
            if team2:
                return team2["id"]
        except Exception:
            pass
    # 4) As last attempt, sport+full name
    try:
        team3 = repo.get_team_by_sport_name_ci(sport, api_team_name or name)  # type: ignore[attr-defined]
        if team3:
            return team3["id"]
    except Exception:
        pass
    raise RuntimeError(f"Team not found in DB for sport={sport} name={name} abbr={abbreviation}")


def _map_status(status: str) -> str:
    s = (status or "").strip().lower()
    # Common short codes from API-Sports: NS, FT, OT, AOT, PST, PPD
    if s in ("ns", "not started", "scheduled", "pre-match", "tbd"):
        return "scheduled"
    if s in ("ft", "final", "finished", "match finished", "full time", "ended", "final/ot", "final/so"):
        return "final"
    if s in ("ot", "aot", "live", "in play", "in progress", "ht", "1h", "2h", "q1", "q2", "q3", "q4"):
        return "live"
    if "postpon" in s or s in ("pst", "ppd", "pp"):
        return "postponed"
    if "cancel" in s or s in ("canc", "cancelled"):
        return "canceled"
    if "suspend" in s:
        return "suspended"
    if "delay" in s or s == "delayed":
        return "delayed"
    return "scheduled"


def ingest_schedule_for_sport(client: httpx.Client, repo: SupabaseRepo | DBRepo, sport: str, league_id: int, season: int, date_str: str) -> Dict[str, Any]:
    base = SPORT_META[sport]["base"]
    # Primary: league+season+date
    r = client.get(f"{base}/games", params={"league": league_id, "season": season, "date": date_str, "timezone": "America/New_York"})
    r.raise_for_status()
    games = r.json().get("response", [])
    # Fallback: date-only then filter
    if not games:
        rf = client.get(f"{base}/games", params={"date": date_str, "timezone": "America/New_York"})
        if rf.status_code == 200:
            all_games = rf.json().get("response", [])
            def _is_match(g: dict) -> bool:
                lid = (g.get("league") or {}).get("id")
                lname = ((g.get("league") or {}).get("name") or "").upper()
                # Accept exact id, exact name, or name that contains the sport token (e.g., "NFL PRESEASON")
                return lid == league_id or lname == sport or (sport in lname)
            games = [g for g in all_games if _is_match(g)]

    # Pre-pass to compute normalized team keys, dates, and derive MLB doubleheaders, NFL week
    prepped: list[dict] = []
    for g in games:
        league = sport
        # API-Sports structures vary per sport; extract safely
        teams = g.get("teams", {})
        home = teams.get("home", {})
        away = teams.get("away", {})
        home_name = home.get("name") or home.get("name_short") or ""
        away_name = away.get("name") or away.get("name_short") or ""
        home_name_norm = normalize_team_name(home_name)
        away_name_norm = normalize_team_name(away_name)
        home_abbr = home.get("name", "")[0:3].upper() if not home.get("code") else home.get("code")
        away_abbr = away.get("name", "")[0:3].upper() if not away.get("code") else away.get("code")

        date_obj = parse_datetime_to_utc(g.get("date") or g.get("time", ""))
        utc_ymd = date_obj.date().isoformat()
        # Build slug using UTC date and hyphenated team_name_only segments when available
        game_slug = build_game_slug(home_abbr, away_abbr, utc_ymd)
        try:
            home_team_row = repo.get_team_by_sport_abbr(sport, home_abbr)  # type: ignore[attr-defined]
            away_team_row = repo.get_team_by_sport_abbr(sport, away_abbr)  # type: ignore[attr-defined]
            if home_team_row and away_team_row:
                h_key_raw = (home_team_row.get("team_name_only") or home_team_row.get("abbreviation") or home_abbr)
                a_key_raw = (away_team_row.get("team_name_only") or away_team_row.get("abbreviation") or away_abbr)
                norm_h = slugify_team_name_only(str(h_key_raw).upper())
                norm_a = slugify_team_name_only(str(a_key_raw).upper())
                game_slug = build_game_slug(norm_h, norm_a, utc_ymd)
            else:
                # Fallback: try by normalized names/team_name_only
                h_row = None
                a_row = None
                try:
                    h_row = repo.get_team_by_team_name_only_ci(sport, derive_team_name_only(home_name_norm))  # type: ignore[attr-defined]
                    a_row = repo.get_team_by_team_name_only_ci(sport, derive_team_name_only(away_name_norm))  # type: ignore[attr-defined]
                except Exception:
                    pass
                if h_row and a_row:
                    norm_h = slugify_team_name_only(str(h_row.get("team_name_only")).upper())
                    norm_a = slugify_team_name_only(str(a_row.get("team_name_only")).upper())
                    game_slug = build_game_slug(norm_h, norm_a, utc_ymd)
                else:
                    norm_h = home_abbr
                    norm_a = away_abbr
        except Exception:
            norm_h = home_abbr
            norm_a = away_abbr

        # Parse NFL week if present
        week_val = None
        if sport == "NFL":
            wk = g.get("week") or g.get("round") or (g.get("league") or {}).get("round")
            if isinstance(wk, int):
                week_val = wk
            elif isinstance(wk, str):
                import re as _re
                m = _re.search(r"(\d+)", wk)
                if m:
                    week_val = int(m.group(1))
            elif isinstance(wk, dict):
                for k in ("number", "round", "week", "value", "name"):
                    v = wk.get(k)
                    if isinstance(v, int):
                        week_val = v
                        break
                    if isinstance(v, str):
                        import re as _re
                        m = _re.search(r"(\d+)", v)
                        if m:
                            week_val = int(m.group(1))
                            break

        prepped.append({
            "g": g,
            "date_obj": date_obj,
            "date_ymd": utc_ymd,
            "home_abbr": home_abbr,
            "away_abbr": away_abbr,
            "norm_h": norm_h,
            "norm_a": norm_a,
            "game_slug": game_slug,
            "home": home,
            "away": away,
            "home_name": home_name,
            "away_name": away_name,
            "home_name_norm": home_name_norm,
            "away_name_norm": away_name_norm,
            "week": week_val,
        })

    # Derive MLB doubleheader sequence by (date, H, A) grouping
    dh_map: dict[tuple[str, str, str], list[dict]] = {}
    if sport == "MLB" and prepped:
        for item in prepped:
            key = (item["date_ymd"], item["norm_h"], item["norm_a"]) 
            dh_map.setdefault(key, []).append(item)
        for key, lst in dh_map.items():
            if len(lst) > 1:
                lst.sort(key=lambda x: x["date_obj"])  # earliest first
                for idx, it in enumerate(lst, start=1):
                    it["doubleheader_seq"] = idx
            else:
                lst[0]["doubleheader_seq"] = None

    results = {"inserted": 0, "updated": 0, "total": len(prepped)}
    for item in prepped:
        g = item["g"]
        league = sport

        api_lid = (g.get("league") or {}).get("id")
        home_team_id = _resolve_team(
            repo, sport, item["home_abbr"], item["home_name"], league,
            api_league_number=api_lid,
            api_team_number=item["home"].get("id"),
            api_team_name=item["home_name_norm"],
        )
        away_team_id = _resolve_team(
            repo, sport, item["away_abbr"], item["away_name"], league,
            api_league_number=api_lid,
            api_team_number=item["away"].get("id"),
            api_team_name=item["away_name_norm"],
        )

        status_text = g.get("status", {}).get("short") or g.get("status", {}).get("long") or g.get("status", "")
        row = {
            "game_id": build_game_slug(item["norm_h"], item["norm_a"], item["date_ymd"],
                                        item.get("doubleheader_seq") if sport == "MLB" else None,
                                        item["week"] if sport == "NFL" else None),
            "league": league,
            "season": season,
            "week": item["week"] if sport == "NFL" else None,
            "start_time_utc": item["date_obj"].isoformat(),
            "start_time_tbd": False,
            "original_start_time_utc": None,
            "venue_id": None,
            "is_neutral_site": False,
            "doubleheader_seq": item.get("doubleheader_seq") if sport == "MLB" else None,
            "rotation_number_home": None,
            "rotation_number_away": None,
            "status": _map_status(status_text),
            "home_team_id": home_team_id,
            "away_team_id": away_team_id,
            "home_score": None,
            "away_score": None,
        }
        # If we have an external ref, prefer updating by id and keep canonical slug
        ext_id = g.get("id") or g.get("game", {}).get("id")
        if ext_id:
            try:
                found = repo.find_game_by_external_ref("api-sports", str(ext_id))  # type: ignore[attr-defined]
            except Exception:
                found = None
            if found:
                # Only update if important fields changed
                updates = {}
                # canonical slug may change after rules tweaks
                updates["game_id"] = row["game_id"]
                updates["start_time_utc"] = row["start_time_utc"]
                updates["status"] = row["status"]
                try:
                    repo.update_game_by_id(found["id"], updates)  # type: ignore[attr-defined]
                    results["updated"] += 1
                except Exception:
                    pass
            else:
                before = repo.insert_game_if_not_exists(row) or {}
                try:
                    repo.upsert_external_ref("game", before.get("id"), "api-sports", str(ext_id))  # type: ignore[attr-defined]
                except Exception:
                    pass
                if before:
                    results["inserted"] += 1
        else:
            before = repo.insert_game_if_not_exists(row)
            if before:
                results["inserted"] += 1
    return results


