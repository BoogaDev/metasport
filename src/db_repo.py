from __future__ import annotations

from typing import Optional, Dict, Any, List
import os
import psycopg
from psycopg.rows import dict_row


class DBRepo:
    def __init__(self, dsn: Optional[str] = None) -> None:
        self.dsn = dsn or os.environ.get("DATABASE_URL")
        if not self.dsn:
            raise RuntimeError("DATABASE_URL not set for DBRepo")
        self.conn = psycopg.connect(self.dsn, autocommit=True, row_factory=dict_row)

    # Teams
    def get_team_by_sport_abbr(self, sport: str, abbreviation: str) -> Optional[Dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                select * from teams
                where upper(sport) = upper(%s) and upper(abbreviation) = upper(%s)
                limit 1
                """,
                (sport, abbreviation),
            )
            return cur.fetchone()

    def get_team_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute("select * from teams where name = %s limit 1", (name,))
            return cur.fetchone()

    def get_team_by_name_ci(self, name: str) -> Optional[Dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute("select * from teams where lower(name) = lower(%s) limit 1", (name,))
            return cur.fetchone()

    def get_team_by_sport_name_ci(self, sport: str, name: str) -> Optional[Dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                "select * from teams where upper(sport)=upper(%s) and upper(name)=upper(%s) limit 1",
                (sport, name),
            )
            return cur.fetchone()

    def get_team_by_team_name_only_ci(self, sport: str, team_name_only: str) -> Optional[Dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                "select * from teams where upper(sport)=upper(%s) and upper(team_name_only)=upper(%s) limit 1",
                (sport, team_name_only),
            )
            return cur.fetchone()

    def list_teams_by_team_name_only_ci(self, sport: str, team_name_only: str) -> List[Dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                "select * from teams where upper(sport)=upper(%s) and upper(team_name_only)=upper(%s)",
                (sport, team_name_only),
            )
            return cur.fetchall() or []

    def get_team_by_id(self, team_id: str) -> Optional[Dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute("select * from teams where id = %s limit 1", (team_id,))
            return cur.fetchone()

    def update_team_api_fields(self, team_id: str, api_league_number: Optional[int], api_team_number: Optional[int], api_team_name: Optional[str]) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                update teams
                   set api_league_number = coalesce(%s, api_league_number),
                       api_team_number = coalesce(%s, api_team_number),
                       api_team_name = coalesce(%s, api_team_name),
                       updated_at = now()
                 where id = %s
                """,
                (api_league_number, api_team_number, api_team_name, team_id),
            )

    def upsert_team(self, row: Dict[str, Any]) -> Dict[str, Any]:
        columns = [
            "name",
            "abbreviation",
            "city",
            "division",
            "conference",
            "logo_url",
            "primary_color",
            "secondary_color",
            "sport",
            "league",
        ]
        values = [row.get(c) for c in columns]
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                insert into teams ({', '.join(columns)})
                values ({', '.join(['%s'] * len(columns))})
                on conflict (sport, abbreviation)
                do update set
                  name = excluded.name,
                  city = excluded.city,
                  division = excluded.division,
                  conference = excluded.conference,
                  logo_url = excluded.logo_url,
                  primary_color = excluded.primary_color,
                  secondary_color = excluded.secondary_color,
                  league = excluded.league,
                  updated_at = now()
                returning *
                """,
                values,
            )
            return cur.fetchone()

    # Games
    def upsert_game(self, row: Dict[str, Any]) -> Dict[str, Any]:
        columns = [
            "game_id",
            "league",
            "season",
            "week",
            "start_time_utc",
            "start_time_tbd",
            "original_start_time_utc",
            "venue_id",
            "is_neutral_site",
            "doubleheader_seq",
            "rotation_number_home",
            "rotation_number_away",
            "status",
            "home_team_id",
            "away_team_id",
            "home_score",
            "away_score",
        ]
        values = [row.get(c) for c in columns]
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                insert into games ({', '.join(columns)})
                values ({', '.join(['%s'] * len(columns))})
                on conflict (game_id)
                do update set
                  start_time_utc = excluded.start_time_utc,
                  status = excluded.status,
                  home_score = excluded.home_score,
                  away_score = excluded.away_score,
                  original_start_time_utc = coalesce(games.original_start_time_utc, excluded.original_start_time_utc),
                  updated_at = now()
                returning *
                """,
                values,
            )
            return cur.fetchone()

    def update_game_scores(self, game_id_slug: str, home_score: Optional[int], away_score: Optional[int], status: Optional[str]) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                update games
                   set home_score = %s,
                       away_score = %s,
                       status = coalesce(%s, status),
                       updated_at = now()
                 where game_id = %s
                """,
                (home_score, away_score, status, game_id_slug),
            )

    def get_game_by_slug(self, game_id_slug: str) -> Optional[Dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute("select id from games where game_id = %s limit 1", (game_id_slug,))
            return cur.fetchone()

    def get_game_by_slug_full(self, game_id_slug: str) -> Optional[Dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                "select id, status, home_score, away_score from games where game_id = %s limit 1",
                (game_id_slug,),
            )
            return cur.fetchone()

    def find_game_by_external_ref(self, provider: str, provider_key: str) -> Optional[Dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                select g.id, g.game_id
                  from external_refs er
                  join games g on g.id = er.entity_id
                 where er.entity_type = 'game'
                   and er.provider = %s
                   and er.provider_key = %s
                 limit 1
                """,
                (provider, provider_key),
            )
            return cur.fetchone()

    def update_game_by_id(self, game_uuid: str, updates: Dict[str, Any]) -> None:
        allowed = [
            "game_id",
            "league",
            "season",
            "week",
            "start_time_utc",
            "start_time_tbd",
            "original_start_time_utc",
            "venue_id",
            "is_neutral_site",
            "doubleheader_seq",
            "rotation_number_home",
            "rotation_number_away",
            "status",
            "home_team_id",
            "away_team_id",
            "home_score",
            "away_score",
        ]
        sets = []
        params = []
        for k in allowed:
            if k in updates:
                sets.append(f"{k} = %s")
                params.append(updates[k])
        if not sets:
            return
        params.append(game_uuid)
        sql = f"update games set {', '.join(sets)}, updated_at = now() where id = %s"
        with self.conn.cursor() as cur:
            cur.execute(sql, params)

    def update_game_scores_by_id(self, game_uuid: str, home_score: Optional[int], away_score: Optional[int], status: Optional[str]) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                update games
                   set home_score = %s,
                       away_score = %s,
                       status = coalesce(%s, status),
                       updated_at = now()
                 where id = %s
                """,
                (home_score, away_score, status, game_uuid),
            )

    # Market selections
    def find_market_selection(self, game_uuid: str, selection_type: str, period: str, participant_team_id: Optional[str], side: str, line: Optional[float], line_units: str) -> Optional[Dict[str, Any]]:
        with self.conn.cursor() as cur:
            sql = [
                "select * from market_selections where game_id = %s and selection_type = %s and period = %s and side = %s and line_units = %s",
            ]
            params: List[Any] = [game_uuid, selection_type, period, side, line_units]
            if participant_team_id is None:
                sql.append("and participant_team_id is null")
            else:
                sql.append("and participant_team_id = %s")
                params.append(participant_team_id)
            if line is None:
                sql.append("and line is null")
            else:
                sql.append("and line = %s")
                params.append(line)
            sql.append("limit 1")
            cur.execute(" ".join(sql), params)
            return cur.fetchone()

    def insert_market_selection(self, row: Dict[str, Any]) -> Dict[str, Any]:
        columns = [
            "game_id",
            "selection_type",
            "period",
            "participant_team_id",
            "side",
            "line",
            "line_units",
        ]
        values = [row.get(c) for c in columns]
        with self.conn.cursor() as cur:
            cur.execute(
                f"""
                insert into market_selections ({', '.join(columns)})
                values ({', '.join(['%s'] * len(columns))})
                returning *
                """,
                values,
            )
            return cur.fetchone()

    # Sportsbooks
    def upsert_sportsbook(self, row: Dict[str, Any]) -> Dict[str, Any]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                insert into sportsbooks (name, slug, region)
                values (%s, %s, %s)
                on conflict (slug)
                do update set name = excluded.name,
                              region = excluded.region,
                              updated_at = now()
                returning *
                """,
                (row.get("name"), row.get("slug"), row.get("region")),
            )
            return cur.fetchone()

    def get_sportsbook_by_slug(self, slug: str) -> Optional[Dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute("select * from sportsbooks where slug = %s limit 1", (slug,))
            return cur.fetchone()

    # Odds
    def insert_odds(self, row: Dict[str, Any]) -> Dict[str, Any]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                insert into odds (market_selection_id, sportsbook_id, timestamp_utc, price_american, limit_amount, source_note)
                values (%s, %s, %s, %s, %s, %s)
                returning *
                """,
                (
                    row.get("market_selection_id"),
                    row.get("sportsbook_id"),
                    row.get("timestamp_utc"),
                    row.get("price_american"),
                    row.get("limit_amount"),
                    row.get("source_note"),
                ),
            )
            return cur.fetchone()

    # External refs
    def upsert_external_ref(self, entity_type: str, entity_id: str, provider: str, provider_key: str) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                insert into external_refs (entity_type, entity_id, provider, provider_key)
                values (%s, %s, %s, %s)
                on conflict (entity_type, provider, provider_key)
                do update set entity_id = excluded.entity_id
                """,
                (entity_type, entity_id, provider, provider_key),
            )

    # Windows
    def list_games_in_window(self, league: str, start_iso_utc: str, end_iso_utc: str) -> List[Dict[str, Any]]:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                select game_id, start_time_utc from games
                 where league = %s and start_time_utc between %s and %s
                """,
                (league, start_iso_utc, end_iso_utc),
            )
            return cur.fetchall() or []


