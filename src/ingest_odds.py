from __future__ import annotations

from typing import Dict, Any, Optional
import re
import httpx
from datetime import datetime, timezone

from .constants import SPORT_META
from .util import decimal_to_american, american_to_decimal, build_game_slug, parse_datetime_to_utc
from .supabase_repo import SupabaseRepo  # for type compatibility
from .db_repo import DBRepo
from .config import ODDS_API_KEY, ODDS_API_REGIONS


def _slugify(name: str) -> str:
    s = name.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def _extract_abbr(team: dict) -> str:
    return team.get("code") or (team.get("name", "")[0:3].upper())


def _ensure_sportsbook(repo: SupabaseRepo, bookmaker: dict) -> str:
    slug = _slugify(bookmaker.get("name") or bookmaker.get("key") or "book")
    found = repo.get_sportsbook_by_slug(slug)
    if found:
        return found["id"]
    row = {"name": bookmaker.get("name") or slug, "slug": slug, "region": None}
    return repo.upsert_sportsbook(row)["id"]


def _get_selection(repo: SupabaseRepo, game_uuid: str, selection_type: str, period: str, participant_team_id: Optional[str], side: str, line: Optional[float], line_units: str) -> str:
    found = repo.find_market_selection(game_uuid, selection_type, period, participant_team_id, side, line, line_units)
    if found:
        return found["id"]
    row = {
        "game_id": game_uuid,
        "selection_type": selection_type,
        "period": period,
        "participant_team_id": participant_team_id,
        "side": side,
        "line": line,
        "line_units": line_units,
    }
    return repo.insert_market_selection(row)["id"]


def ingest_odds_for_sport(client: httpx.Client, repo: SupabaseRepo | DBRepo, sport: str, league_id: int, season: int, date_str: str) -> Dict[str, Any]:
    line_units = SPORT_META[sport]["line_units"]
    # The Odds API mapping
    sport_key_map = {"MLB": "baseball_mlb", "NHL": "icehockey_nhl", "NFL": "americanfootball_nfl"}
    if not ODDS_API_KEY:
        return {"inserted": 0, "events": 0}
    sport_key = sport_key_map[sport]
    # Fetch odds for all upcoming events, then filter by date
    odds_url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds"
    start_iso = f"{date_str}T00:00:00Z"
    end_iso = f"{date_str}T23:59:59Z"
    r = client.get(odds_url, params={
        "apiKey": ODDS_API_KEY,
        "regions": ODDS_API_REGIONS,
        "oddsFormat": "american",
        "dateFormat": "iso",
        "markets": "h2h,spreads,totals",
        "bookmakers": "draftkings",
        "commenceTimeFrom": start_iso,
        "commenceTimeTo": end_iso,
    })
    if r.status_code != 200:
        return {"inserted": 0, "events": 0}
    events = r.json() if isinstance(r.json(), list) else []
    # Keep only pregame events (kickoff in the future)
    now_utc = datetime.now(timezone.utc)
    def _ct(ev):
        try:
            return parse_datetime_to_utc(ev.get("commence_time"))
        except Exception:
            return None
    events = [e for e in events if (ct := _ct(e)) and ct > now_utc]
    results = {"inserted": 0, "events": len(events)}
    for ev in events:
        home_name = ev.get("home_team")
        away_name = ev.get("away_team")
        commence_dt = parse_datetime_to_utc(ev.get("commence_time")) if ev.get("commence_time") else None
        # Safety: still enforce pregame only
        if not commence_dt or commence_dt <= now_utc:
            continue
        date_iso = commence_dt.date().isoformat()
        # Resolve teams by (sport, name)
        home_row = None
        away_row = None
        try:
            home_row = repo.get_team_by_sport_name_ci(sport, home_name)  # type: ignore[attr-defined]
            away_row = repo.get_team_by_sport_name_ci(sport, away_name)  # type: ignore[attr-defined]
        except Exception:
            # SupabaseRepo fallback by name only
            try:
                hr = repo.get_team_by_name(home_name)  # type: ignore[attr-defined]
                ar = repo.get_team_by_name(away_name)  # type: ignore[attr-defined]
                if hr and hr.get("sport") == sport:
                    home_row = hr
                if ar and ar.get("sport") == sport:
                    away_row = ar
            except Exception:
                pass
        if not (home_row and away_row):
            continue
        h_key = (home_row.get("team_name_only") or home_row.get("abbreviation")).upper()
        a_key = (away_row.get("team_name_only") or away_row.get("abbreviation")).upper()
        slug = build_game_slug(h_key, a_key, date_iso)
        # Find game by external ref or slug
        game_uuid = None
        event_id = ev.get("id")
        try:
            found = repo.find_game_by_external_ref("the-odds-api", str(event_id))  # type: ignore[attr-defined]
        except Exception:
            found = None
        if found:
            game_uuid = found["id"]
        else:
            try:
                row = repo.get_game_by_slug(slug)  # type: ignore[attr-defined]
                game_uuid = row["id"] if row else None
            except Exception:
                game_uuid = None
        if not game_uuid:
            continue
        # persist external ref
        if event_id:
            try:
                repo.upsert_external_ref("game", game_uuid, "the-odds-api", str(event_id))  # type: ignore[attr-defined]
            except Exception:
                pass
        # iterate bookmakers/markets
        for book in (ev.get("bookmakers", []) or []):
            bk_key = (book.get("key") or book.get("title") or "").lower()
            if bk_key not in ("draftkings",):
                continue
            sportsbook_id = _ensure_sportsbook(repo, book)
            ts = book.get("last_update") or book.get("updated")
            # Enforce pregame lines only per bookmaker: skip if the book's last update is at/after kickoff
            if ts:
                try:
                    ts_dt = parse_datetime_to_utc(ts)
                    if commence_dt and ts_dt >= commence_dt:
                        continue
                except Exception:
                    pass
            ts_utc = parse_datetime_to_utc(ts).isoformat() if ts else datetime.now(timezone.utc).isoformat()
            for market in (book.get("markets", []) or []):
                mkey = (market.get("key") or market.get("name", "")).lower()
                outcomes = market.get("outcomes") or []
                if not outcomes:
                    continue
                if mkey in ("h2h", "moneyline"):
                    for o in outcomes:
                        name = (o.get("name") or "").lower()
                        price = o.get("price")
                        american = int(price) if isinstance(price, (int, float)) else None
                        side = "home" if name == (home_name or "").lower() else ("away" if name == (away_name or "").lower() else None)
                        if side is None or american is None:
                            continue
                        team_id = (home_row["id"] if side == "home" else away_row["id"]) if (home_row and away_row) else None
                        sel_id = _get_selection(repo, game_uuid, "moneyline", "full_game", team_id, side, None, line_units)
                        repo.insert_odds({
                            "market_selection_id": sel_id,
                            "sportsbook_id": sportsbook_id,
                            "timestamp_utc": ts_utc,
                            "price_american": american,
                            "price_european": american_to_decimal(american),
                            "limit_amount": None,
                            "source_note": "the-odds-api",
                        })
                        results["inserted"] += 1
                elif mkey in ("spreads", "spread"):
                    for o in outcomes:
                        name = (o.get("name") or "").lower()
                        price = o.get("price")
                        point = o.get("point")
                        if price is None or point is None:
                            continue
                        american = int(price) if isinstance(price, (int, float)) else None
                        if american is None:
                            continue
                        side = "home" if name == (home_name or "").lower() else ("away" if name == (away_name or "").lower() else None)
                        if side is None:
                            continue
                        team_id = (home_row["id"] if side == "home" else away_row["id"]) if (home_row and away_row) else None
                        sel_id = _get_selection(repo, game_uuid, "spread", "full_game", team_id, side, float(point), line_units)
                        repo.insert_odds({
                            "market_selection_id": sel_id,
                            "sportsbook_id": sportsbook_id,
                            "timestamp_utc": ts_utc,
                            "price_american": american,
                            "price_european": american_to_decimal(american),
                            "limit_amount": None,
                            "source_note": "the-odds-api",
                        })
                        results["inserted"] += 1
                elif mkey in ("totals", "over_under", "ou"):
                    for o in outcomes:
                        label = (o.get("name") or "").lower()
                        price = o.get("price")
                        point = o.get("point")
                        if price is None or point is None:
                            continue
                        american = int(price) if isinstance(price, (int, float)) else None
                        side = "over" if "over" in label else ("under" if "under" in label else None)
                        if side is None or american is None:
                            continue
                        sel_id = _get_selection(repo, game_uuid, "total", "full_game", None, side, float(point), line_units)
                        repo.insert_odds({
                            "market_selection_id": sel_id,
                            "sportsbook_id": sportsbook_id,
                            "timestamp_utc": ts_utc,
                            "price_american": american,
                            "price_european": american_to_decimal(american),
                            "limit_amount": None,
                            "source_note": "the-odds-api",
                        })
                        results["inserted"] += 1
    return results


def ingest_odds_historical_for_sport(
    client: httpx.Client,
    repo: SupabaseRepo | DBRepo,
    sport: str,
    date_str: str,
    snapshots_iso: list[str],
) -> Dict[str, Any]:
    line_units = SPORT_META[sport]["line_units"]
    sport_key_map = {"MLB": "baseball_mlb", "NHL": "icehockey_nhl", "NFL": "americanfootball_nfl"}
    if not ODDS_API_KEY:
        return {"inserted": 0, "events": 0}
    sport_key = sport_key_map[sport]
    # Fetch historical events for the day window
    start_iso = f"{date_str}T00:00:00Z"
    end_iso = f"{date_str}T23:59:59Z"
    ev_url = f"https://api.the-odds-api.com/v4/historical/sports/{sport_key}/events"
    ev = client.get(ev_url, params={
        "apiKey": ODDS_API_KEY,
        "dateFormat": "iso",
        "commenceTimeFrom": start_iso,
        "commenceTimeTo": end_iso,
    })
    if ev.status_code != 200:
        return {"inserted": 0, "events": 0}
    events = ev.json() if isinstance(ev.json(), list) else []
    total_inserted = 0
    for e in events:
        event_id = e.get("id")
        if not event_id:
            continue
        home_name = e.get("home_team")
        away_name = e.get("away_team")
        date_iso = (e.get("commence_time") or "").split("T")[0]
        # Resolve teams by sport+name
        try:
            home_row = repo.get_team_by_sport_name_ci(sport, home_name)  # type: ignore[attr-defined]
            away_row = repo.get_team_by_sport_name_ci(sport, away_name)  # type: ignore[attr-defined]
        except Exception:
            continue
        if not (home_row and away_row):
            continue
        h_key = (home_row.get("team_name_only") or home_row.get("abbreviation")).upper()
        a_key = (away_row.get("team_name_only") or away_row.get("abbreviation")).upper()
        slug = build_game_slug(h_key, a_key, date_iso)
        # find game uuid (prefer external ref)
        game_uuid = None
        try:
            found = repo.find_game_by_external_ref("the-odds-api", str(event_id))  # type: ignore[attr-defined]
        except Exception:
            found = None
        if found:
            game_uuid = found["id"]
        else:
            try:
                row = repo.get_game_by_slug(slug)  # type: ignore[attr-defined]
                game_uuid = row["id"] if row else None
            except Exception:
                game_uuid = None
        if not game_uuid:
            continue
        # persist mapping
        try:
            repo.upsert_external_ref("game", game_uuid, "the-odds-api", str(event_id))  # type: ignore[attr-defined]
        except Exception:
            pass
        # Pull each snapshot
        hist_url = f"https://api.the-odds-api.com/v4/historical/sports/{sport_key}/events/{event_id}/odds"
        for snap in snapshots_iso:
            r = client.get(hist_url, params={
                "apiKey": ODDS_API_KEY,
                "regions": ODDS_API_REGIONS,
                "oddsFormat": "american",
                "dateFormat": "iso",
                "markets": "h2h,spreads,totals",
                "bookmakers": "draftkings",
                "date": snap,
            })
            if r.status_code != 200:
                continue
            js = r.json() or {}
            data = js.get("data") or {}
            # Skip snapshots at or after kickoff to enforce pregame odds only
            try:
                kickoff = parse_datetime_to_utc(data.get("commence_time")) if data.get("commence_time") else None
                snap_dt = parse_datetime_to_utc(js.get("timestamp")) if js.get("timestamp") else None
                if kickoff and snap_dt and snap_dt >= kickoff:
                    continue
            except Exception:
                pass
            for book in (data.get("bookmakers", []) or []):
                bk_key = (book.get("key") or book.get("title") or "").lower()
                if bk_key not in ("draftkings",):
                    continue
                sportsbook_id = _ensure_sportsbook(repo, book)
                ts = book.get("last_update") or book.get("updated")
                ts_utc = parse_datetime_to_utc(ts).isoformat() if ts else datetime.now(timezone.utc).isoformat()
                for market in (book.get("markets", []) or []):
                    mkey = (market.get("key") or market.get("name", "")).lower()
                    outcomes = market.get("outcomes") or []
                    if not outcomes:
                        continue
                    if mkey in ("h2h", "moneyline"):
                        for o in outcomes:
                            name = (o.get("name") or "").lower()
                            price = o.get("price")
                            american = int(price) if isinstance(price, (int, float)) else None
                            side = "home" if name == (home_name or "").lower() else ("away" if name == (away_name or "").lower() else None)
                            if side is None or american is None:
                                continue
                            team_id = (home_row["id"] if side == "home" else away_row["id"]) if (home_row and away_row) else None
                            sel_id = _get_selection(repo, game_uuid, "moneyline", "full_game", team_id, side, None, line_units)
                            repo.insert_odds({
                                "market_selection_id": sel_id,
                                "sportsbook_id": sportsbook_id,
                                "timestamp_utc": ts_utc,
                                "price_american": american,
                                "price_european": american_to_decimal(american),
                                "limit_amount": None,
                                "source_note": "the-odds-api",
                            })
                            total_inserted += 1
                    elif mkey in ("spreads", "spread"):
                        for o in outcomes:
                            name = (o.get("name") or "").lower()
                            price = o.get("price")
                            point = o.get("point")
                            if price is None or point is None:
                                continue
                            american = int(price) if isinstance(price, (int, float)) else None
                            if american is None:
                                continue
                            side = "home" if name == (home_name or "").lower() else ("away" if name == (away_name or "").lower() else None)
                            if side is None:
                                continue
                            team_id = (home_row["id"] if side == "home" else away_row["id"]) if (home_row and away_row) else None
                            sel_id = _get_selection(repo, game_uuid, "spread", "full_game", team_id, side, float(point), line_units)
                            repo.insert_odds({
                                "market_selection_id": sel_id,
                                "sportsbook_id": sportsbook_id,
                                "timestamp_utc": ts_utc,
                                "price_american": american,
                                "price_european": american_to_decimal(american),
                                "limit_amount": None,
                                "source_note": "the-odds-api",
                            })
                            total_inserted += 1
                    elif mkey in ("totals", "over_under", "ou"):
                        for o in outcomes:
                            label = (o.get("name") or "").lower()
                            price = o.get("price")
                            point = o.get("point")
                            if price is None or point is None:
                                continue
                            american = int(price) if isinstance(price, (int, float)) else None
                            side = "over" if "over" in label else ("under" if "under" in label else None)
                            if side is None or american is None:
                                continue
                            sel_id = _get_selection(repo, game_uuid, "total", "full_game", None, side, float(point), line_units)
                            repo.insert_odds({
                                "market_selection_id": sel_id,
                                "sportsbook_id": sportsbook_id,
                                "timestamp_utc": ts_utc,
                                "price_american": american,
                                "price_european": american_to_decimal(american),
                                "limit_amount": None,
                                "source_note": "the-odds-api",
                            })
                            total_inserted += 1
    return {"inserted": total_inserted, "events": len(events)}
    results = {"inserted": 0, "events": len(events)}
    for ev in events:
        teams = ev.get("teams", {})
        home = teams.get("home", {})
        away = teams.get("away", {})
        home_abbr = _extract_abbr(home)
        away_abbr = _extract_abbr(away)
        date_obj = parse_datetime_to_utc(ev.get("date") or ev.get("time", ""))
        slug = build_game_slug(home_abbr, away_abbr, date_obj.date().isoformat())

        # Need the game uuid to tie market selections → odds. Fetch the game row.
        game_row = None
        try:
            game_row = repo.get_game_by_slug(slug)  # type: ignore[attr-defined]
        except Exception:
            game_row = None
        if not game_row:
            # ensure schedule ingested first
            continue
        game_uuid = game_row["id"]
        # Link external id for odds event if present so future joins are easier
        ext_id = ev.get("id") or ev.get("game", {}).get("id")
        if ext_id:
            try:
                repo.upsert_external_ref("game", game_uuid, "api-sports", str(ext_id))  # type: ignore[attr-defined]
            except Exception:
                pass

        for book in ev.get("bookmakers", []):
            sportsbook_id = _ensure_sportsbook(repo, book)
            ts = book.get("updated") or book.get("lastUpdate")
            ts_utc = parse_datetime_to_utc(ts).isoformat() if ts else datetime.now(timezone.utc).isoformat()
            for market in book.get("bets", []) or book.get("markets", []):
                mkey = market.get("key") or market.get("name", "").lower()
                outcomes = market.get("values") or market.get("outcomes") or []
                if not outcomes:
                    continue
                if mkey in ("h2h", "moneyline"):
                    for o in outcomes:
                        name = o.get("team") or o.get("name") or ""
                        price = o.get("odd") or o.get("price")
                        dec = float(price) if isinstance(price, (int, float, str)) else None
                        if dec is None:
                            continue
                        american = decimal_to_american(float(dec))
                        side = "home" if name.lower() == (home.get("name", "").lower()) else "away"
                        pid = game_uuid if False else None
                        # participant_team_id must be team UUID; look up by slug
                        team_id = None
                        if side == "home":
                            team_row = repo.get_team_by_sport_abbr(sport, home_abbr)
                            team_id = team_row["id"] if team_row else None
                        else:
                            team_row = repo.get_team_by_sport_abbr(sport, away_abbr)
                            team_id = team_row["id"] if team_row else None
                        sel_id = _get_selection(repo, game_uuid, "moneyline", "full_game", team_id, side, None, line_units)
                        repo.insert_odds({
                            "market_selection_id": sel_id,
                            "sportsbook_id": sportsbook_id,
                            "timestamp_utc": ts_utc,
                            "price_american": american,
                            "limit_amount": None,
                            "source_note": "api-sports",
                        })
                        results["inserted"] += 1
                elif mkey in ("spreads", "spread"):
                    for o in outcomes:
                        name = o.get("team") or o.get("name") or ""
                        price = o.get("odd") or o.get("price")
                        handicap = o.get("handicap") or o.get("point") or o.get("line")
                        if price is None or handicap is None:
                            continue
                        american = decimal_to_american(float(price))
                        side = "home" if name.lower() == (home.get("name", "").lower()) else "away"
                        team_row = repo.get_team_by_sport_abbr(sport, home_abbr if side == "home" else away_abbr)
                        team_id = team_row["id"] if team_row else None
                        sel_id = _get_selection(repo, game_uuid, "spread", "full_game", team_id, side, float(handicap), line_units)
                        repo.insert_odds({
                            "market_selection_id": sel_id,
                            "sportsbook_id": sportsbook_id,
                            "timestamp_utc": ts_utc,
                            "price_american": american,
                            "limit_amount": None,
                            "source_note": "api-sports",
                        })
                        results["inserted"] += 1
                elif mkey in ("totals", "over_under", "ou"):
                    for o in outcomes:
                        label = (o.get("name") or o.get("label") or "").lower()
                        price = o.get("odd") or o.get("price")
                        total = o.get("total") or o.get("point") or o.get("line")
                        if price is None or total is None:
                            continue
                        american = decimal_to_american(float(price))
                        side = "over" if "over" in label else "under"
                        sel_id = _get_selection(repo, game_uuid, "total", "full_game", None, side, float(total), line_units)
                        repo.insert_odds({
                            "market_selection_id": sel_id,
                            "sportsbook_id": sportsbook_id,
                            "timestamp_utc": ts_utc,
                            "price_american": american,
                            "limit_amount": None,
                            "source_note": "api-sports",
                        })
                        results["inserted"] += 1
    return results


