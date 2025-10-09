from __future__ import annotations

"""
Standalone DraftKings odds scraper and ingester.

Features
- Captures DraftKings NHL page screenshots (cards or chunks) via Playwright
- Calls OpenAI (vision) with your exact prompt, embedding teams_rows.json and
  llmjsonoutput.json as guidance
- Parses model JSON into a canonical structure; rounds start times to :00/:30
- Inserts market selections and odds into your database (Supabase Postgres)

Environment
- OPENAI_API_KEY: required for the OpenAI client
- DATABASE_URL: preferred (postgres connection string)
  OR
- SUPABASE_URL + SUPABASE_SERVICE_ROLE: fallback if DATABASE_URL is not set

Usage (examples)
  python3 dk_odds.py --selection today --mode chunks \
    --teams ./teams_rows.json --example ./llmjsonoutput.json

  python3 dk_odds.py --selection tomorrow --mode cards \
    --teams ./teams_rows.json --example ./llmjsonoutput.json \
    --debug_raw ./artifacts/agent_raw.txt

The script writes screenshots under ./artifacts/ and a final
llmjsonoutput_*.json next to the script.
"""

import os
import json
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytz
from dateutil import parser as dtparser
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
from openai import OpenAI
from dotenv import load_dotenv


LOCAL_TZ = pytz.timezone("America/Los_Angeles")
DK_URL = "https://sportsbook.draftkings.com/leagues/hockey/nhl"
DEFAULT_LINE_UNITS = "goals"  # NHL
DEFAULT_SPORTSBOOK_SLUG = "draftkings"
DEFAULT_SPORTSBOOK_NAME = "DraftKings"

# Load environment variables from a local .env if present
load_dotenv()


# ---------- Utilities ----------

def ensure_dir(p: Path) -> None:
    p.mkdir(parents=True, exist_ok=True)


def now_ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def b64_data_url(png_path: str) -> str:
    import base64

    b = Path(png_path).read_bytes()
    return "data:image/png;base64," + base64.b64encode(b).decode()


def american_to_decimal(american_odds: Optional[int | float | str]) -> Optional[float]:
    if american_odds is None:
        return None
    if isinstance(american_odds, str):
        s = american_odds.strip().lower()
        if s == "ev":
            american_odds = 100
        else:
            try:
                american_odds = float(american_odds)
            except Exception:
                return None
    a = float(american_odds)
    if a >= 100:
        return round(1.0 + (a / 100.0), 4)
    if a <= -100:
        return round(1.0 + (100.0 / abs(a)), 4)
    return None


def round_to_half_hour(dt_local: datetime) -> datetime:
    m = dt_local.minute
    if m < 15:
        rr = 0
    elif m < 45:
        rr = 30
    else:
        dt_local = dt_local + timedelta(hours=1)
        rr = 0
    return dt_local.replace(minute=rr, second=0, microsecond=0)


def normalize_team_name(full_name: str) -> str:
    return (full_name or "").strip().upper()


def slugify_team_name_only(name_only_upper: str) -> str:
    s = (name_only_upper or "").strip().upper()
    return s.replace(" ", "-")


def build_game_slug(away_team_name_only: str, home_team_name_only: str, date_yyyy_mm_dd: str) -> str:
    """Build game_id with AWAY first then HOME to match DB convention.

    Example: CANADIENS_MAPLE-LEAFS_2025-10-08
    """
    return f"{slugify_team_name_only(away_team_name_only)}_{slugify_team_name_only(home_team_name_only)}_{date_yyyy_mm_dd}"


# ---------- Screenshot capture ----------

def dismiss_modals(page) -> None:
    for sel in [
        'button:has-text("Maybe later")',
        'button:has-text("No thanks")',
        'button:has-text("Not now")',
        'button:has-text("Got it")',
        'button[aria-label="Close"]',
        'button:has-text("Accept")',
    ]:
        try:
            page.locator(sel).first.click(timeout=1500)
        except Exception:
            pass


def wait_page_ready(page) -> None:
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_load_state("networkidle")
    page.wait_for_timeout(1200)


def autoscroll_all(page, pause_ms: int = 400) -> None:
    last = 0
    while True:
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(pause_ms)
        curr = page.evaluate("document.body.scrollHeight")
        if curr == last:
            break
        last = curr
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(300)


def screenshot_cards(page, selector: Optional[str], outdir: Path) -> List[Path]:
    if selector:
        try:
            page.wait_for_selector(selector, timeout=5000)
        except PwTimeout:
            selector = None

    candidates = [
        selector,
        "div.sportsbook-event-accordion__event-row",
        "div.event-card",
        "div.sportsbook-event-card",
        "div[data-testid='event-row']",
        "[data-qa='event-row']",
        "a:has-text('More Bets') >> xpath=ancestor::div[contains(@class,'event')]",
        "a:has-text('More Bets') >> xpath=ancestor::div[1]",
    ]
    sel_use = None
    for sel in candidates:
        if not sel:
            continue
        try:
            page.wait_for_selector(sel, timeout=3000)
            sel_use = sel
            break
        except PwTimeout:
            continue
    if not sel_use:
        # dump for debugging
        Path(f"dk_debug_{now_ts()}.html").write_text(page.content())
        raise RuntimeError("Could not find event rows; wrote dk_debug_*.html")

    cards = page.locator(sel_use)
    count = cards.count()
    if count == 0:
        raise RuntimeError("Zero cards found for selector: " + sel_use)
    saved: List[Path] = []
    for i in range(count):
        el = cards.nth(i)
        try:
            el.scroll_into_view_if_needed()
            page.wait_for_timeout(150)
            fname = outdir / f"game_{i+1:02d}_{now_ts()}.png"
            el.screenshot(path=str(fname))
            print(f"Saved {fname.name}")
            saved.append(fname)
        except Exception as e:
            print(f"[WARN] card {i+1}: {e}")
    return saved


def screenshot_chunks(page, outdir: Path, width: int = 1400, chunk_h: int = 1600, overlap: int = 220) -> List[Path]:
    page.set_viewport_size({"width": width, "height": chunk_h})
    total = page.evaluate("document.body.scrollHeight")
    y, idx, saved = 0, 1, []
    while y < total:
        page.evaluate(f"window.scrollTo(0, {y})")
        page.wait_for_timeout(250)
        fname = outdir / f"chunk_{idx:02d}_{now_ts()}.png"
        page.screenshot(path=str(fname), full_page=False)
        saved.append(fname)
        print(f"Saved {fname.name} (y={y})")
        y += chunk_h - overlap
        idx += 1
    page.evaluate("window.scrollTo(0, 0)")
    return saved


# ---------- OpenAI calls ----------

def call_openai(images: List[Path], teams_json: dict, example_json: dict, date_selection: str, debug_path: Optional[Path] = None) -> dict:
    client = OpenAI()

    # Build content parts: instruction + example docs + images
    system_prompt = (
        "Your role is a data entry analyst. You need to parse the screenshots for "
        f"{date_selection} date.  You may look up any information needed for logic but not raw data. "
        "You should return a json structure that follows the llmjsonoutput.json structure that "
        "references the teams_rows.json table. You must make sure all teams and data are scrapped "
        "for todays date and any math done needed to calculate european odds is correct. "
        "You should follow the data types and structure of llmjsonoutput.json and start times should "
        "make sure they are rounded to the nearest hour or half hour i.e. a 4:10PM start time should be 4:00PM "
        "and 7:45PM start time should be 7:30PM. All Team Names should be the full name. "
        "Return ONLY JSON."
    )

    # Convert images to base64 data URLs
    img_parts = [
        {"type": "image_url", "image_url": {"url": b64_data_url(str(p))}} for p in images
    ]

    # Provide example files as text references
    example_parts = [
        {"type": "text", "text": "teams_rows.json:"},
        {"type": "text", "text": json.dumps(teams_json, indent=2)},
        {"type": "text", "text": "llmjsonoutput.json (schema example):"},
        {"type": "text", "text": json.dumps(example_json, indent=2)},
    ]

    raw: Optional[str] = None

    # Prefer Chat Completions for broad support
    try:
        resp = client.chat.completions.create(
            model="gpt-5",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": example_parts + img_parts},
            ],
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content if resp and resp.choices else "{}"
    except Exception:
        raw = "{}"

    if debug_path:
        try:
            debug_path.write_text(raw or "")
        except Exception:
            pass

    # Parse JSON
    try:
        data = json.loads(raw or "{}")
    except Exception:
        s = (raw or "")
        s = s[s.find("{") : s.rfind("}") + 1]
        data = json.loads(s or "{}")
    return data


# ---------- DB adapters ----------


class DBBase:
    def get_or_create_sportsbook(self, name: str, slug: str) -> str:
        raise NotImplementedError

    def lookup_team_by_name(self, full_name_upper: str) -> Optional[dict]:
        raise NotImplementedError

    def get_game_uuid_by_slug(self, game_slug: str) -> Optional[str]:
        raise NotImplementedError

    def find_market_selection(
        self,
        game_uuid: str,
        selection_type: str,
        period: str,
        participant_team_id: Optional[str],
        side: str,
        line: Optional[float],
        line_units: str,
    ) -> Optional[str]:
        raise NotImplementedError

    def insert_market_selection(
        self,
        game_uuid: str,
        selection_type: str,
        period: str,
        participant_team_id: Optional[str],
        side: str,
        line: Optional[float],
        line_units: str,
    ) -> str:
        raise NotImplementedError

    def insert_odds(
        self,
        market_selection_id: str,
        sportsbook_id: str,
        timestamp_utc_iso: str,
        price_american: int,
        price_european: Optional[float],
        source_note: str,
    ) -> None:
        raise NotImplementedError

    def list_game_slugs_for_date(self, league: str, date_iso: str) -> List[str]:
        raise NotImplementedError


class DBPostgres(DBBase):
    def __init__(self, dsn: str) -> None:
        import psycopg
        from psycopg.rows import dict_row

        self.conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
        # Ensure we read from the expected schema
        try:
            with self.conn.cursor() as cur:
                cur.execute("set search_path to public")
        except Exception:
            pass

    def get_or_create_sportsbook(self, name: str, slug: str) -> str:
        with self.conn.cursor() as cur:
            cur.execute("select id from sportsbooks where slug = %s limit 1", (slug,))
            row = cur.fetchone()
            if row:
                return row["id"]
            cur.execute(
                """
                insert into sportsbooks (name, slug)
                values (%s, %s)
                on conflict (slug) do update set name = excluded.name, updated_at = now()
                returning id
                """,
                (name, slug),
            )
            return cur.fetchone()["id"]

    def lookup_team_by_name(self, full_name_upper: str) -> Optional[dict]:
        with self.conn.cursor() as cur:
            cur.execute(
                "select id, name, team_name_only from teams where upper(name)=upper(%s) limit 1",
                (full_name_upper,),
            )
            return cur.fetchone()

    def get_game_uuid_by_slug(self, game_slug: str) -> Optional[str]:
        with self.conn.cursor() as cur:
            cur.execute("select id from games where game_id = %s limit 1", (game_slug,))
            row = cur.fetchone()
            return row["id"] if row else None

    def find_market_selection(
        self,
        game_uuid: str,
        selection_type: str,
        period: str,
        participant_team_id: Optional[str],
        side: str,
        line: Optional[float],
        line_units: str,
    ) -> Optional[str]:
        sql = [
            "select id from market_selections where game_id=%s and selection_type=%s and period=%s and side=%s and line_units=%s",
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
        with self.conn.cursor() as cur:
            cur.execute(" ".join(sql), params)
            row = cur.fetchone()
            return row["id"] if row else None

    def insert_market_selection(
        self,
        game_uuid: str,
        selection_type: str,
        period: str,
        participant_team_id: Optional[str],
        side: str,
        line: Optional[float],
        line_units: str,
    ) -> str:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                insert into market_selections (game_id, selection_type, period, participant_team_id, side, line, line_units)
                values (%s, %s, %s, %s, %s, %s, %s)
                returning id
                """,
                (game_uuid, selection_type, period, participant_team_id, side, line, line_units),
            )
            return cur.fetchone()["id"]

    def insert_odds(
        self,
        market_selection_id: str,
        sportsbook_id: str,
        timestamp_utc_iso: str,
        price_american: int,
        price_european: Optional[float],
        source_note: str,
    ) -> None:
        with self.conn.cursor() as cur:
            cur.execute(
                """
                insert into odds (market_selection_id, sportsbook_id, timestamp_utc, price_american, price_european, limit_amount, source_note)
                values (%s, %s, %s, %s, %s, %s, %s)
                """,
                (market_selection_id, sportsbook_id, timestamp_utc_iso, int(price_american), price_european, None, source_note),
            )

    def list_game_slugs_for_date(self, league: str, date_iso: str) -> List[str]:
        with self.conn.cursor() as cur:
            cur.execute(
                "select game_id from games where league=%s and start_time_utc::date=%s order by game_id",
                (league, date_iso),
            )
            rows = cur.fetchall() or []
            return [r["game_id"] for r in rows]


class DBSupabase(DBBase):
    def __init__(self, url: str, key: str) -> None:
        from supabase import create_client, Client

        self.client: Client = create_client(url, key)

    def get_or_create_sportsbook(self, name: str, slug: str) -> str:
        q = self.client.table("sportsbooks").select("id").eq("slug", slug).limit(1)
        data = (q.execute().data or [])
        if data:
            return data[0]["id"]
        ins = self.client.table("sportsbooks").insert({"name": name, "slug": slug}).execute()
        if ins.data:
            return ins.data[0]["id"]
        # one more try (race)
        data = (q.execute().data or [])
        return data[0]["id"]

    def lookup_team_by_name(self, full_name_upper: str) -> Optional[dict]:
        q = self.client.table("teams").select("id,name,team_name_only").eq("name", full_name_upper).limit(1)
        data = (q.execute().data or [])
        return data[0] if data else None

    def get_game_uuid_by_slug(self, game_slug: str) -> Optional[str]:
        q = self.client.table("games").select("id").eq("game_id", game_slug).limit(1)
        data = (q.execute().data or [])
        return data[0]["id"] if data else None

    def find_market_selection(
        self,
        game_uuid: str,
        selection_type: str,
        period: str,
        participant_team_id: Optional[str],
        side: str,
        line: Optional[float],
        line_units: str,
    ) -> Optional[str]:
        q = (
            self.client.table("market_selections")
            .select("id")
            .eq("game_id", game_uuid)
            .eq("selection_type", selection_type)
            .eq("period", period)
            .eq("side", side)
            .eq("line_units", line_units)
        )
        if participant_team_id is None:
            q = q.is_("participant_team_id", "null")
        else:
            q = q.eq("participant_team_id", participant_team_id)
        if line is None:
            q = q.is_("line", "null")
        else:
            q = q.eq("line", line)
        data = (q.limit(1).execute().data or [])
        return data[0]["id"] if data else None

    def insert_market_selection(
        self,
        game_uuid: str,
        selection_type: str,
        period: str,
        participant_team_id: Optional[str],
        side: str,
        line: Optional[float],
        line_units: str,
    ) -> str:
        row = {
            "game_id": game_uuid,
            "selection_type": selection_type,
            "period": period,
            "participant_team_id": participant_team_id,
            "side": side,
            "line": line,
            "line_units": line_units,
        }
        res = self.client.table("market_selections").insert(row).execute()
        return (res.data or [{}])[0].get("id")

    def insert_odds(
        self,
        market_selection_id: str,
        sportsbook_id: str,
        timestamp_utc_iso: str,
        price_american: int,
        price_european: Optional[float],
        source_note: str,
    ) -> None:
        row = {
            "market_selection_id": market_selection_id,
            "sportsbook_id": sportsbook_id,
            "timestamp_utc": timestamp_utc_iso,
            "price_american": int(price_american),
            "price_european": price_european,
            "limit_amount": None,
            "source_note": source_note,
        }
        self.client.table("odds").insert(row).execute()

    def list_game_slugs_for_date(self, league: str, date_iso: str) -> List[str]:
        q = (
            self.client.table("games")
            .select("game_id")
            .eq("league", league)
            .eq("start_time_utc::date", date_iso)  # some clients support ::date in RPC, Supabase REST may not
        )
        try:
            data = q.execute().data or []
        except Exception:
            # fallback: fetch a range and filter client-side
            data = (
                self.client.table("games")
                .select("game_id,start_time_utc")
                .eq("league", league)
                .execute()
                .data
                or []
            )
            data = [d for d in data if str(d.get("start_time_utc", "")).split("T")[0] == date_iso]
        return [d.get("game_id") for d in data if d.get("game_id")]


def get_db() -> DBBase:
    if os.environ.get("DATABASE_URL"):
        return DBPostgres(os.environ["DATABASE_URL"])
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE") or os.environ.get("SUPABASE_KEY")
    if url and key:
        return DBSupabase(url, key)
    raise RuntimeError("Set DATABASE_URL or SUPABASE_URL + SUPABASE_SERVICE_ROLE in environment.")


# ---------- Parsing and insertion ----------

def _find_games_in_structure(obj: Any, depth: int = 0) -> List[dict]:
    if depth > 4:
        return []
    if isinstance(obj, list):
        if obj and isinstance(obj[0], dict) and ("home_team" in obj[0] or "away_team" in obj[0] or "market" in obj[0]):
            return obj  # likely the games list
        for it in obj:
            res = _find_games_in_structure(it, depth + 1)
            if res:
                return res
        return []
    if isinstance(obj, dict):
        if isinstance(obj.get("games"), list):
            return obj.get("games") or []
        for key in ("output_parsed", "final_output", "output", "data", "result", "response"):
            if key in obj:
                val = obj.get(key)
                try:
                    if isinstance(val, str):
                        val = json.loads(val)
                except Exception:
                    pass
                res = _find_games_in_structure(val, depth + 1)
                if res:
                    return res
        for v in obj.values():
            res = _find_games_in_structure(v, depth + 1)
            if res:
                return res
    if isinstance(obj, str):
        try:
            parsed = json.loads(obj)
            return _find_games_in_structure(parsed, depth + 1)
        except Exception:
            return []
    return []

def load_team_map(teams_rows: dict) -> Tuple[dict, dict]:
    name_to_id: dict[str, str] = {}
    name_to_team_name_only: dict[str, str] = {}
    for r in teams_rows:
        full_upper = normalize_team_name(r.get("name", ""))
        tid = str(r.get("id"))
        tno = normalize_team_name(r.get("team_name_only", ""))
        if full_upper:
            name_to_id[full_upper] = tid
            name_to_team_name_only[full_upper] = tno
    return name_to_id, name_to_team_name_only


def ensure_market_selection(db: DBBase, game_uuid: str, selection_type: str, period: str, participant_team_id: Optional[str], side: str, line: Optional[float], line_units: str) -> str:
    found = db.find_market_selection(game_uuid, selection_type, period, participant_team_id, side, line, line_units)
    if found:
        return found
    return db.insert_market_selection(game_uuid, selection_type, period, participant_team_id, side, line, line_units)


def ingest_from_json(db: DBBase, payload: dict, teams_rows: dict, verbose: bool = False) -> dict:
    name_to_id, name_to_tno = load_team_map(teams_rows)

    games = payload.get("games")
    if not isinstance(games, list):
        games = _find_games_in_structure(payload)
    if not isinstance(games, list):
        games = []
    if verbose:
        print(f"[INFO] games found: {len(games)}")

    sportsbook_id = None
    # Prefer the ID from payload if present; otherwise ensure DraftKings exists by slug
    if payload.get("sportsbook_id"):
        sportsbook_id = str(payload["sportsbook_id"])  # type: ignore[index]
    else:
        sportsbook_id = db.get_or_create_sportsbook(DEFAULT_SPORTSBOOK_NAME, DEFAULT_SPORTSBOOK_SLUG)

    inserted_odds = 0
    created_selections = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    tz_name = (payload.get("time_zone") or payload.get("timezone") or "America/New_York").replace(" ", "_")
    try:
        TARGET_TZ = pytz.timezone(tz_name)
    except Exception:
        TARGET_TZ = pytz.timezone("America/New_York")

    for g in games:
        try:
            # Support shapes where markets are nested under g["market"]
            src_markets = g.get("market") if isinstance(g, dict) else None
            # Teams
            away_full = normalize_team_name(g.get("away_team") or (g.get("away") or ""))
            home_full = normalize_team_name(g.get("home_team") or (g.get("home") or ""))
            away_id = name_to_id.get(away_full)
            home_id = name_to_id.get(home_full)
            if not (away_id and home_id):
                if verbose:
                    print(f"[SKIP team map] {away_full} @ {home_full}")
                continue

            # Parse/round local start time then derive date (UTC date used for slug)
            dt_local = None
            if g.get("start_time_local"):
                dt_local = dtparser.parse(g["start_time_local"])  # type: ignore[index]
            elif g.get("start_time"):
                # Provided in UTC. Convert to the target timezone (ET by default) then round.
                try:
                    dt_utc_tmp = dtparser.parse(g["start_time"])  # type: ignore[index]
                    if dt_utc_tmp.tzinfo is None:
                        dt_utc_tmp = pytz.UTC.localize(dt_utc_tmp)
                    dt_local = dt_utc_tmp.astimezone(TARGET_TZ)
                except Exception:
                    dt_local = None
            if dt_local is None:
                if verbose:
                    print(f"[WARN] missing start_time; inferring from now")
                dt_local = datetime.now(LOCAL_TZ)
            if dt_local.tzinfo is None:
                dt_local = LOCAL_TZ.localize(dt_local)
            dt_local = round_to_half_hour(dt_local)
            dt_utc = dt_local.astimezone(pytz.UTC)
            # Use TARGET_TZ date in slug to match DB convention
            date_iso = dt_local.date().isoformat()

            # Build slug consistent with your games table convention
            home_tno = name_to_tno.get(home_full) or home_full.split(" ")[-1]
            away_tno = name_to_tno.get(away_full) or away_full.split(" ")[-1]
            slug = build_game_slug(away_tno, home_tno, date_iso)
            game_uuid = db.get_game_uuid_by_slug(slug)
            if not game_uuid:
                if verbose:
                    print(f"[SKIP game lookup] slug={slug}")
                continue

            # Moneyline odds (accept dicts like {away:{price_american:..}} or plain ints)
            ml = (src_markets.get("moneyline") if isinstance(src_markets, dict) else None) or g.get("moneyline") or {}
            def _val(x):
                if isinstance(x, dict):
                    # try common keys
                    return x.get("price_american") or x.get("american") or x.get("price")
                return x
            ml_away = _val(ml.get("away"))
            ml_home = _val(ml.get("home"))
            if ml_away is not None:
                sel = ensure_market_selection(db, game_uuid, "moneyline", "full_game", away_id, "away", None, DEFAULT_LINE_UNITS)
                db.insert_odds(sel, sportsbook_id, now_iso, int(ml_away), american_to_decimal(ml_away), "dk-screenshots")
                inserted_odds += 1
            if ml_home is not None:
                sel = ensure_market_selection(db, game_uuid, "moneyline", "full_game", home_id, "home", None, DEFAULT_LINE_UNITS)
                db.insert_odds(sel, sportsbook_id, now_iso, int(ml_home), american_to_decimal(ml_home), "dk-screenshots")
                inserted_odds += 1

            # Spread odds
            spread_container = src_markets if isinstance(src_markets, dict) else g
            # Spread: accept several shapes
            spread = spread_container.get("puck_line") or spread_container.get("spread") or {}
            sp_away_line = spread.get("away_line")
            sp_home_line = spread.get("home_line")
            # Accept numeric away/home directly
            if sp_away_line is None and isinstance(spread.get("away"), (int, float, str)):
                try:
                    sp_away_line = float(spread.get("away"))
                except Exception:
                    pass
            if sp_home_line is None and isinstance(spread.get("home"), (int, float, str)):
                try:
                    sp_home_line = float(spread.get("home"))
                except Exception:
                    pass
            spread_odds = spread_container.get("spread_odds") or {}
            sp_away_price = _val(spread_odds.get("away")) if spread_odds else _val(spread.get("away_price"))
            sp_home_price = _val(spread_odds.get("home")) if spread_odds else _val(spread.get("home_price"))
            # Also accept nested dicts like spread={away:{line:+1.5, price:-170}, home:{line:-1.5, price:+140}}
            if (sp_away_line is None or sp_away_price is None) and isinstance(spread.get("away"), dict):
                sp_away_line = spread.get("away", {}).get("line", sp_away_line)
                sp_away_price = _val(spread.get("away", {}).get("price", sp_away_price))
            if (sp_home_line is None or sp_home_price is None) and isinstance(spread.get("home"), dict):
                sp_home_line = spread.get("home", {}).get("line", sp_home_line)
                sp_home_price = _val(spread.get("home", {}).get("price", sp_home_price))
            if sp_away_line is not None and sp_away_price is not None:
                sel = ensure_market_selection(db, game_uuid, "spread", "full_game", away_id, "away", float(sp_away_line), DEFAULT_LINE_UNITS)
                db.insert_odds(sel, sportsbook_id, now_iso, int(sp_away_price), american_to_decimal(sp_away_price), "dk-screenshots")
                inserted_odds += 1
            if sp_home_line is not None and sp_home_price is not None:
                sel = ensure_market_selection(db, game_uuid, "spread", "full_game", home_id, "home", float(sp_home_line), DEFAULT_LINE_UNITS)
                db.insert_odds(sel, sportsbook_id, now_iso, int(sp_home_price), american_to_decimal(sp_home_price), "dk-screenshots")
                inserted_odds += 1

            # Totals odds
            totals = (spread_container.get("total_goals") if isinstance(spread_container, dict) else None) or g.get("total_goals") or {}
            tg = totals
            tnum = tg.get("number")
            t_odds_container = spread_container if isinstance(spread_container, dict) else g
            t_odds = t_odds_container.get("total_goals_odds") or {}
            over_p = _val(t_odds.get("over"))
            under_p = _val(t_odds.get("under"))
            if tnum is not None and over_p is not None:
                sel = ensure_market_selection(db, game_uuid, "total", "full_game", None, "over", float(tnum), DEFAULT_LINE_UNITS)
                db.insert_odds(sel, sportsbook_id, now_iso, int(over_p), american_to_decimal(over_p), "dk-screenshots")
                inserted_odds += 1
            if tnum is not None and under_p is not None:
                sel = ensure_market_selection(db, game_uuid, "total", "full_game", None, "under", float(tnum), DEFAULT_LINE_UNITS)
                db.insert_odds(sel, sportsbook_id, now_iso, int(under_p), american_to_decimal(under_p), "dk-screenshots")
                inserted_odds += 1

            if verbose and ml_away is None and ml_home is None and sp_away_line is None and sp_home_line is None and tnum is None:
                print(f"[INFO] no markets parsed for {away_full} @ {home_full}")

        except Exception as exc:
            print(f"[WARN] game processing error: {exc}")
            continue

    return {"inserted": inserted_odds}


# ---------- CLI ----------

def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--selection", choices=["today", "tomorrow"], default="today")
    ap.add_argument("--mode", choices=["cards", "chunks"], default="chunks")
    ap.add_argument("--selector", default=None, help="Optional CSS selector for cards mode")
    ap.add_argument("--width", type=int, default=1400)
    ap.add_argument("--height", type=int, default=2000)
    ap.add_argument("--chunk_height", type=int, default=1100)
    ap.add_argument("--overlap", type=int, default=220)
    ap.add_argument("--scale", type=int, default=4, help="Device scale factor for sharper screenshots (default 4)")
    ap.add_argument("--teams", default=str(Path(__file__).parent / "teams_rows.json"))
    ap.add_argument("--example", default=str(Path(__file__).parent / "llmjsonoutput.json"))
    ap.add_argument("--debug_raw", default=None)
    ap.add_argument("--outdir", default=str(Path(__file__).parent / "artifacts" / f"dk_caps_{now_ts()}"))
    ap.add_argument("--dry_run", action="store_true", help="Do not write to DB; only print result counts")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    # Prepare directories
    outdir = Path(args.outdir)
    ensure_dir(outdir)

    # Load reference files
    teams_path = Path(args.teams)
    example_path = Path(args.example)
    if not teams_path.exists():
        raise RuntimeError(f"Missing teams_rows.json at {teams_path}")
    if not example_path.exists():
        raise RuntimeError(f"Missing llmjsonoutput.json at {example_path}")
    teams_rows = json.loads(teams_path.read_text())
    llm_example = json.loads(example_path.read_text())

    # Capture screenshots
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context(
            viewport={"width": args.width, "height": args.height},
            device_scale_factor=args.scale,
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        print("Navigating…")
        try:
            page.goto(DK_URL, wait_until="domcontentloaded", timeout=10000)
            wait_page_ready(page)
            dismiss_modals(page)
            # Scrolling helps lazy load
            autoscroll_all(page)
        except Exception:
            pass

        if args.mode == "chunks":
            images = screenshot_chunks(page, outdir, width=args.width, chunk_h=args.chunk_height, overlap=args.overlap)
        else:
            images = screenshot_cards(page, args.selector, outdir)
        browser.close()

    print("\nScreenshots:")
    for pth in images:
        print("-", pth)

    # Call OpenAI
    debug_path = Path(args.debug_raw) if args.debug_raw else None
    model_output = call_openai(images, teams_rows, llm_example, args.selection, debug_path=debug_path)

    # Persist model output for review
    out_json = Path(__file__).parent / f"llmjsonoutput_{now_ts()}.json"
    out_json.write_text(json.dumps(model_output, indent=2))
    print(f"Wrote {out_json}")

    if args.dry_run:
        print("[DRY RUN] Skipping DB inserts.")
        return 0

    # DB inserts
    db = get_db()
    res = ingest_from_json(db, model_output, teams_rows, verbose=args.verbose)
    print({"odds_inserted": res.get("inserted", 0)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
