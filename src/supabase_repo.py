from __future__ import annotations

from typing import Optional, Dict, Any
from supabase import Client


class SupabaseRepo:
    def __init__(self, client: Client, schema: str = "public") -> None:
        self.client = client
        self.schema = schema

    # Teams
    def get_team_by_sport_abbr(self, sport: str, abbreviation: str) -> Optional[Dict[str, Any]]:
        res = (
            self.client.table("teams")
            .select("*")
            .eq("sport", sport)
            .eq("abbreviation", abbreviation)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        return rows[0] if rows else None

    def get_team_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        res = self.client.table("teams").select("*").eq("name", name).limit(1).execute()
        rows = res.data or []
        return rows[0] if rows else None

    def upsert_team(self, row: Dict[str, Any]) -> Dict[str, Any]:
        # on_conflict expects column list, not constraint name
        res = self.client.table("teams").upsert(row, on_conflict="sport,abbreviation").execute()
        return res.data[0] if res.data else row

    # Games
    def upsert_game(self, row: Dict[str, Any]) -> Dict[str, Any]:
        res = self.client.table("games").upsert(row, on_conflict="game_id").execute()
        return res.data[0] if res.data else row

    def update_game_scores(self, game_id_slug: str, home_score: Optional[int], away_score: Optional[int], status: Optional[str]) -> None:
        q = self.client.table("games").update({"home_score": home_score, "away_score": away_score, "status": status}).eq("game_id", game_id_slug)
        q.execute()

    # Market selections
    def find_market_selection(self, game_uuid: str, selection_type: str, period: str, participant_team_id: Optional[str], side: str, line: Optional[float], line_units: str) -> Optional[Dict[str, Any]]:
        q = (
            self.client.table("market_selections")
            .select("*")
            .eq("game_id", game_uuid)
            .eq("selection_type", selection_type)
            .eq("period", period)
            .eq("side", side)
            .eq("line_units", line_units)
        )
        if participant_team_id is None:
            q = q.is_("participant_team_id", None)
        else:
            q = q.eq("participant_team_id", participant_team_id)
        if line is None:
            q = q.is_("line", None)
        else:
            q = q.eq("line", line)
        res = q.limit(1).execute()
        rows = res.data or []
        return rows[0] if rows else None

    def insert_market_selection(self, row: Dict[str, Any]) -> Dict[str, Any]:
        res = self.client.table("market_selections").insert(row).execute()
        return res.data[0]

    # Sportsbooks
    def upsert_sportsbook(self, row: Dict[str, Any]) -> Dict[str, Any]:
        res = self.client.table("sportsbooks").upsert(row, on_conflict="slug").execute()
        return res.data[0] if res.data else row

    def get_sportsbook_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        res = self.client.table("sportsbooks").select("*").eq("slug", slug).limit(1).execute()
        rows = res.data or []
        return rows[0] if rows else None

    # Odds
    def insert_odds(self, row: Dict[str, Any]) -> Dict[str, Any]:
        res = self.client.table("odds").insert(row).execute()
        return res.data[0]


