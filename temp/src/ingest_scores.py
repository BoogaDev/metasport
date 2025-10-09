from __future__ import annotations

from typing import Dict, Any, Optional
import httpx

from .constants import SPORT_META
from .supabase_repo import SupabaseRepo  # for type compatibility
from .db_repo import DBRepo
from .util import parse_datetime_to_utc, build_game_slug, deep_sum_numbers, slugify_team_name_only
from zoneinfo import ZoneInfo
from .config import TIMEZONE


def _extract_abbr(team: dict) -> str:
    return team.get("code") or (team.get("name", "")[0:3].upper())


def _map_status(status: str) -> str:
    s = (status or "").strip().lower()
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


def ingest_scores_for_sport(
    client: httpx.Client,
    repo: SupabaseRepo | DBRepo,
    sport: str,
    league_id: int,
    season: int,
    date_str: Optional[str] = None,
) -> Dict[str, Any]:
    base = SPORT_META[sport]["base"]
    params = {"league": league_id, "season": season}
    if date_str:
        params.update({"date": date_str, "timezone": "America/New_York"})
    else:
        params.update({"live": "all"})
    r = client.get(f"{base}/games", params=params)
    r.raise_for_status()
    games = r.json().get("response", [])
    # If we asked for live and got none, fall back to date today to catch finals
    if not games and not date_str:
        from datetime import datetime
        today = datetime.now().date().isoformat()
        r2 = client.get(f"{base}/games", params={"league": league_id, "season": season, "date": today, "timezone": "America/New_York"})
        if r2.status_code == 200:
            games = r2.json().get("response", [])
    results = {"updated": 0, "total": len(games)}
    for g in games:
        teams = g.get("teams", {})
        home = teams.get("home", {})
        away = teams.get("away", {})
        scores = g.get("scores", {})
        home_val = scores.get("home")
        away_val = scores.get("away")
        home_score = (home_val or {}).get("total") if isinstance(home_val, dict) else home_val
        away_score = (away_val or {}).get("total") if isinstance(away_val, dict) else away_val
        if home_score is None and isinstance(home_val, (dict, list)):
            home_score = deep_sum_numbers(home_val)
        if away_score is None and isinstance(away_val, (dict, list)):
            away_score = deep_sum_numbers(away_val)
        home_abbr = _extract_abbr(home)
        away_abbr = _extract_abbr(away)
        date_obj = parse_datetime_to_utc(g.get("date") or g.get("time", ""))
        # Prefer external ref mapping when present
        status_text = g.get("status", {}).get("short") or g.get("status", {}).get("long") or g.get("status", "")
        ext_id = g.get("id") or (g.get("game") or {}).get("id")
        updated = False
        if ext_id:
            try:
                found = repo.find_game_by_external_ref("api-sports", str(ext_id))  # type: ignore[attr-defined]
            except Exception:
                found = None
            if found:
                try:
                    repo.update_game_scores_by_id(found["id"], home_score, away_score, _map_status(status_text))  # type: ignore[attr-defined]
                    updated = True
                except Exception:
                    updated = False
        if not updated:
            # Build slug using team_name_only when available
            try:
                home_team_row = repo.get_team_by_sport_abbr(sport, home_abbr)  # type: ignore[attr-defined]
                away_team_row = repo.get_team_by_sport_abbr(sport, away_abbr)  # type: ignore[attr-defined]
                h_key_raw = (home_team_row or {}).get("team_name_only") or home_abbr
                a_key_raw = (away_team_row or {}).get("team_name_only") or away_abbr
                norm_h = slugify_team_name_only(str(h_key_raw).upper())
                norm_a = slugify_team_name_only(str(a_key_raw).upper())
                local_ymd = date_obj.astimezone(ZoneInfo(TIMEZONE)).date().isoformat()
                utc_ymd = date_obj.date().isoformat()
                # Prioritize UTC slug first to align with schedule's UTC-based game_id
                slug_utc = build_game_slug(norm_h, norm_a, utc_ymd)
                slug_local = build_game_slug(norm_h, norm_a, local_ymd)
            except Exception:
                local_ymd = date_obj.astimezone(ZoneInfo(TIMEZONE)).date().isoformat()
                utc_ymd = date_obj.date().isoformat()
                slug_utc = build_game_slug(slugify_team_name_only(home_abbr), slugify_team_name_only(away_abbr), utc_ymd)
                slug_local = build_game_slug(slugify_team_name_only(home_abbr), slugify_team_name_only(away_abbr), local_ymd)
            # Guard: do not overwrite a final game with None scores or regress status
            try:
                existing = repo.get_game_by_slug_full(slug_utc)  # type: ignore[attr-defined]
                hit_slug = slug_utc if existing else None
                if not existing:
                    existing = repo.get_game_by_slug_full(slug_local)  # type: ignore[attr-defined]
                    if existing:
                        hit_slug = slug_local
            except Exception:
                existing = None
                hit_slug = None
            safe_update = existing is not None
            if existing:
                ex_status = (existing.get("status") or "").lower()
                # if existing is final, only update if we have non-null scores
                if ex_status == "final" and (home_score is None or away_score is None):
                    safe_update = False
            if safe_update:
                repo.update_game_scores(hit_slug, home_score, away_score, _map_status(status_text))  # type: ignore[arg-type]
            # else: game row doesn't exist or not safe to update → skip; never insert
        results["updated"] += 1
    return results


