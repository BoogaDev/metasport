from __future__ import annotations

"""
Self-contained DraftKings odds scraper and ingester (portable app).
See README.md for setup and usage.
"""

import os
import sys
import json
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytz
from dateutil import parser as dtparser
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
from openai import OpenAI
from dotenv import load_dotenv


LOCAL_TZ = pytz.timezone("America/Los_Angeles")
# Pin the Games > Game Lines tab explicitly. As of 2026-09-19 the bare league URL
# lands on the Futures tab, which has no moneyline/puck-line/total cards.
DK_URL = "https://sportsbook.draftkings.com/leagues/hockey/nhl?category=games&subcategory=game-lines"
# DraftKings lists exhibition games under a separate league. Off-season / in-season
# it renders an empty board (a couple of chunks), so scraping it year-round is cheap.
DK_PRESEASON_URL = "https://sportsbook.draftkings.com/leagues/hockey/nhl-preseason"
DK_PAGES = (("nhl", DK_URL), ("pre", DK_PRESEASON_URL))
DEFAULT_LINE_UNITS = "goals"  # NHL
DEFAULT_SPORTSBOOK_SLUG = "draftkings"
DEFAULT_SPORTSBOOK_NAME = "DraftKings"

# Load environment variables
load_dotenv()


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
    return f"{slugify_team_name_only(away_team_name_only)}_{slugify_team_name_only(home_team_name_only)}_{date_yyyy_mm_dd}"


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


def apply_css_zoom(page, zoom: float) -> None:
    try:
        page.evaluate(f"document.body.style.zoom='{zoom}'")
        page.wait_for_timeout(200)
    except Exception:
        pass


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


def screenshot_chunks(page, outdir: Path, width: int = 1400, chunk_h: int = 1100, overlap: int = 220, prefix: str = "") -> List[Path]:
    page.set_viewport_size({"width": width, "height": chunk_h})
    total = page.evaluate("document.body.scrollHeight")
    y, idx, saved = 0, 1, []
    tag = f"{prefix}_" if prefix else ""
    while y < total:
        page.evaluate(f"window.scrollTo(0, {y})")
        page.wait_for_timeout(250)
        fname = outdir / f"{tag}chunk_{idx:02d}_{now_ts()}.png"
        page.screenshot(path=str(fname), full_page=False)
        saved.append(fname)
        print(f"Saved {fname.name} (y={y})")
        y += chunk_h - overlap
        idx += 1
    page.evaluate("window.scrollTo(0, 0)")
    return saved


def call_openai(images: List[Path], teams_json: dict, example_json: dict, date_selection: str, debug_path: Optional[Path] = None) -> dict:
    client = OpenAI()

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

    img_parts = [
        {"type": "image_url", "image_url": {"url": b64_data_url(str(p))}} for p in images
    ]

    example_parts = [
        {"type": "text", "text": "teams_rows.json:"},
        {"type": "text", "text": json.dumps(teams_json, indent=2)},
        {"type": "text", "text": "llmjsonoutput.json (schema example):"},
        {"type": "text", "text": json.dumps(example_json, indent=2)},
    ]

    raw: Optional[str] = None
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
    except Exception as e:
        # Do NOT swallow this. A failed LLM call used to degrade to "{}" -> "games
        # found: 0" -> exit 0, which hid an exhausted OpenAI balance for days.
        print(f"[ERROR] OpenAI call failed ({type(e).__name__}): {e}", file=sys.stderr)
        raise

    if debug_path:
        try:
            debug_path.write_text(raw or "")
        except Exception:
            pass

    try:
        data = json.loads(raw or "{}")
    except Exception:
        s = (raw or "")
        s = s[s.find("{") : s.rfind("}") + 1]
        data = json.loads(s or "{}")
    return data


class DBBase:
    def get_or_create_sportsbook(self, name: str, slug: str) -> str: ...
    def lookup_team_by_name(self, full_name_upper: str) -> Optional[dict]: ...
    def get_game_uuid_by_slug(self, game_slug: str) -> Optional[str]: ...
    def find_market_selection(self, game_uuid: str, selection_type: str, period: str, participant_team_id: Optional[str], side: str, line: Optional[float], line_units: str) -> Optional[str]: ...
    def insert_market_selection(self, game_uuid: str, selection_type: str, period: str, participant_team_id: Optional[str], side: str, line: Optional[float], line_units: str) -> str: ...
    def insert_odds(self, market_selection_id: str, sportsbook_id: str, timestamp_utc_iso: str, price_american: int, price_european: Optional[float], source_note: str) -> None: ...
    def list_game_slugs_for_date(self, league: str, date_iso: str) -> List[str]: ...
    def has_odds_for_selection(self, market_selection_id: str, sportsbook_id: str) -> bool: ...
    def get_game_team_ids(self, game_uuid: str) -> Optional[Tuple[str, str]]: ...


class DBPostgres(DBBase):
    def __init__(self, dsn: str) -> None:
        import psycopg
        from psycopg.rows import dict_row

        self.conn = psycopg.connect(dsn, autocommit=True, row_factory=dict_row)
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

    def find_market_selection(self, game_uuid: str, selection_type: str, period: str, participant_team_id: Optional[str], side: str, line: Optional[float], line_units: str) -> Optional[str]:
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

    def insert_market_selection(self, game_uuid: str, selection_type: str, period: str, participant_team_id: Optional[str], side: str, line: Optional[float], line_units: str) -> str:
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

    def insert_odds(self, market_selection_id: str, sportsbook_id: str, timestamp_utc_iso: str, price_american: int, price_european: Optional[float], source_note: str) -> None:
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

    def has_odds_for_selection(self, market_selection_id: str, sportsbook_id: str) -> bool:
        with self.conn.cursor() as cur:
            cur.execute(
                "select 1 from odds where market_selection_id=%s and sportsbook_id=%s limit 1",
                (market_selection_id, sportsbook_id),
            )
            return cur.fetchone() is not None

    def get_game_team_ids(self, game_uuid: str) -> Optional[Tuple[str, str]]:
        with self.conn.cursor() as cur:
            cur.execute(
                "select home_team_id, away_team_id from games where id = %s limit 1",
                (game_uuid,),
            )
            row = cur.fetchone()
            if not row:
                return None
            return (row["home_team_id"], row["away_team_id"])  # type: ignore[index]


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

    def find_market_selection(self, game_uuid: str, selection_type: str, period: str, participant_team_id: Optional[str], side: str, line: Optional[float], line_units: str) -> Optional[str]:
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

    def insert_market_selection(self, game_uuid: str, selection_type: str, period: str, participant_team_id: Optional[str], side: str, line: Optional[float], line_units: str) -> str:
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

    def insert_odds(self, market_selection_id: str, sportsbook_id: str, timestamp_utc_iso: str, price_american: int, price_european: Optional[float], source_note: str) -> None:
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
            .eq("start_time_utc::date", date_iso)
        )
        try:
            data = q.execute().data or []
        except Exception:
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

    def has_odds_for_selection(self, market_selection_id: str, sportsbook_id: str) -> bool:
        q = (
            self.client.table("odds")
            .select("id")
            .eq("market_selection_id", market_selection_id)
            .eq("sportsbook_id", sportsbook_id)
            .limit(1)
        )
        try:
            data = q.execute().data or []
        except Exception:
            data = []
        return bool(data)

    def get_game_team_ids(self, game_uuid: str) -> Optional[Tuple[str, str]]:
        q = (
            self.client.table("games")
            .select("home_team_id,away_team_id")
            .eq("id", game_uuid)
            .limit(1)
        )
        try:
            data = q.execute().data or []
        except Exception:
            data = []
        if not data:
            return None
        row = data[0]
        return (row.get("home_team_id"), row.get("away_team_id"))


def get_db() -> DBBase:
    if os.environ.get("DATABASE_URL"):
        return DBPostgres(os.environ["DATABASE_URL"])
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE") or os.environ.get("SUPABASE_KEY")
    if url and key:
        return DBSupabase(url, key)
    raise RuntimeError("Set DATABASE_URL or SUPABASE_URL + SUPABASE_SERVICE_ROLE in environment.")


def _find_games_in_structure(obj: Any, depth: int = 0) -> List[dict]:
    if depth > 4:
        return []
    if isinstance(obj, list):
        if obj and isinstance(obj[0], dict) and ("home_team" in obj[0] or "away_team" in obj[0] or "market" in obj[0]):
            return obj
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


def _dedupe_model_output(model_output: dict, verbose: bool = False) -> dict:
    """Return a copy of model_output with duplicate games removed.

    Deduplication key preference:
      1) game_id if present
      2) fallback slug built from last word of away/home and local date
    First occurrence wins.
    """
    games = model_output.get("games") or []
    if not isinstance(games, list):
        return model_output

    tz_name = (model_output.get("time_zone") or model_output.get("timezone") or "America/New_York").replace(" ", "_")
    try:
        tz = pytz.timezone(tz_name)
    except Exception:
        tz = pytz.timezone("America/New_York")

    kept = {}
    unique = []
    for g in games:
        key = g.get("game_id")
        if not key:
            # Build fallback key
            away_full = (g.get("away_team") or g.get("away") or "").strip().upper()
            home_full = (g.get("home_team") or g.get("home") or "").strip().upper()
            away_last = away_full.split(" ")[-1] if away_full else ""
            home_last = home_full.split(" ")[-1] if home_full else ""
            # date
            try:
                if g.get("start_time_local"):
                    dt_local = dtparser.parse(g.get("start_time_local"))
                    if dt_local.tzinfo is None:
                        dt_local = tz.localize(dt_local)
                elif g.get("start_time"):
                    dt_utc = dtparser.parse(g.get("start_time"))
                    if dt_utc.tzinfo is None:
                        dt_utc = pytz.UTC.localize(dt_utc)
                    dt_local = dt_utc.astimezone(tz)
                elif g.get("game_date"):
                    dt_local = tz.localize(datetime.fromisoformat(str(g.get("game_date"))))
                else:
                    dt_local = datetime.now(tz)
                date_iso = round_to_half_hour(dt_local).date().isoformat()
            except Exception:
                date_iso = datetime.now(tz).date().isoformat()
            key = build_game_slug(away_last, home_last, date_iso)
        if key in kept:
            continue
        kept[key] = True
        unique.append(g)

    if verbose:
        try:
            print(f"[INFO] dedupe: {len(games)} -> {len(unique)} games")
        except Exception:
            pass
    out = dict(model_output)
    out["games"] = unique
    return out


def ingest_from_json(db: DBBase, payload: dict, teams_rows: dict, verbose: bool = False, only_missing: bool = False, only_games: Optional[List[str]] = None) -> dict:
    name_to_id, name_to_tno = load_team_map(teams_rows)

    games = payload.get("games")
    if not isinstance(games, list):
        games = _find_games_in_structure(payload)
    if not isinstance(games, list):
        games = []
    if verbose:
        print(f"[INFO] games found: {len(games)}")

    sportsbook_id = payload.get("sportsbook_id") or db.get_or_create_sportsbook(DEFAULT_SPORTSBOOK_NAME, DEFAULT_SPORTSBOOK_SLUG)

    inserted_odds = 0
    now_iso = datetime.now(timezone.utc).isoformat()
    tz_name = (payload.get("time_zone") or payload.get("timezone") or "America/New_York").replace(" ", "_")
    try:
        TARGET_TZ = pytz.timezone(tz_name)
    except Exception:
        TARGET_TZ = pytz.timezone("America/New_York")

    def _val(x):
        if isinstance(x, dict):
            return x.get("price_american") or x.get("american") or x.get("price")
        return x

    for g in games:
        try:
            src_markets = g.get("market") if isinstance(g, dict) else None
            away_full = normalize_team_name(g.get("away_team") or (g.get("away") or ""))
            home_full = normalize_team_name(g.get("home_team") or (g.get("home") or ""))
            # Prefer explicit IDs from the JSON if present, then fall back to name mapping
            away_id = g.get("away_team_id") or name_to_id.get(away_full)
            home_id = g.get("home_team_id") or name_to_id.get(home_full)
            if not (away_id and home_id):
                if verbose:
                    print(f"[SKIP team map] {away_full} @ {home_full}")
                continue

            dt_local = None
            if g.get("start_time_local"):
                dt_local = dtparser.parse(g["start_time_local"])  # type: ignore[index]
            elif g.get("start_time"):
                try:
                    dt_utc_tmp = dtparser.parse(g["start_time"])  # type: ignore[index]
                    if dt_utc_tmp.tzinfo is None:
                        dt_utc_tmp = pytz.UTC.localize(dt_utc_tmp)
                    dt_local = dt_utc_tmp.astimezone(TARGET_TZ)
                except Exception:
                    dt_local = None
            if dt_local is None:
                dt_local = datetime.now(LOCAL_TZ)
            if dt_local.tzinfo is None:
                dt_local = LOCAL_TZ.localize(dt_local)
            dt_local = round_to_half_hour(dt_local)
            date_iso = dt_local.date().isoformat()

            home_tno = name_to_tno.get(home_full) or home_full.split(" ")[-1]
            away_tno = name_to_tno.get(away_full) or away_full.split(" ")[-1]
            # Also compute last-word fallbacks (e.g., RED WINGS -> WINGS)
            home_last = (home_full.split(" ")[-1] if home_full else home_tno)
            away_last = (away_full.split(" ")[-1] if away_full else away_tno)

            g_slug = (g.get("game_id") or "").strip()
            candidate_slugs = ([g_slug] if g_slug else []) + [
                build_game_slug(away_tno, home_tno, date_iso),
                build_game_slug(away_last, home_last, date_iso),
                build_game_slug(away_last, home_tno, date_iso),
                build_game_slug(away_tno, home_last, date_iso),
            ]
            game_uuid = None
            for cand in candidate_slugs:
                game_uuid = db.get_game_uuid_by_slug(cand)
                if game_uuid:
                    break
            if not game_uuid:
                if verbose:
                    print(f"[SKIP game lookup] tried={candidate_slugs}")
                continue

            ml = (src_markets.get("moneyline") if isinstance(src_markets, dict) else None) or g.get("moneyline") or {}
            ml_away = _val(ml.get("away"))
            ml_home = _val(ml.get("home"))
            if ml_away is not None:
                sel = ensure_market_selection(db, game_uuid, "moneyline", "full_game", away_id, "away", None, DEFAULT_LINE_UNITS)
                if (not only_missing) or (only_missing and not db.has_odds_for_selection(sel, sportsbook_id)):
                    db.insert_odds(sel, sportsbook_id, now_iso, int(ml_away), american_to_decimal(ml_away), "dk-screenshots")
                inserted_odds += 1
            if ml_home is not None:
                sel = ensure_market_selection(db, game_uuid, "moneyline", "full_game", home_id, "home", None, DEFAULT_LINE_UNITS)
                if (not only_missing) or (only_missing and not db.has_odds_for_selection(sel, sportsbook_id)):
                    db.insert_odds(sel, sportsbook_id, now_iso, int(ml_home), american_to_decimal(ml_home), "dk-screenshots")
                inserted_odds += 1

            spread_container = src_markets if isinstance(src_markets, dict) else g
            spread = spread_container.get("puck_line") or spread_container.get("spread") or {}
            sp_away_line = spread.get("away_line")
            sp_home_line = spread.get("home_line")
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
            if (sp_away_line is None or sp_away_price is None) and isinstance(spread.get("away"), dict):
                sp_away_line = spread.get("away", {}).get("line", sp_away_line)
                sp_away_price = _val(spread.get("away", {}).get("price", sp_away_price))
            if (sp_home_line is None or sp_home_price is None) and isinstance(spread.get("home"), dict):
                sp_home_line = spread.get("home", {}).get("line", sp_home_line)
                sp_home_price = _val(spread.get("home", {}).get("price", sp_home_price))
            # Cross-check team IDs from games to ensure correct home/away assignment
            ids = db.get_game_team_ids(game_uuid) or (home_id, away_id)
            home_uuid_from_game, away_uuid_from_game = ids
            # If mismatch detected, swap
            if away_uuid_from_game and away_id and away_uuid_from_game != away_id:
                away_id = away_uuid_from_game
            if home_uuid_from_game and home_id and home_uuid_from_game != home_id:
                home_id = home_uuid_from_game

            if sp_away_line is not None and sp_away_price is not None:
                sel = ensure_market_selection(db, game_uuid, "spread", "full_game", away_id, "away", float(sp_away_line), DEFAULT_LINE_UNITS)
                if (not only_missing) or (only_missing and not db.has_odds_for_selection(sel, sportsbook_id)):
                    db.insert_odds(sel, sportsbook_id, now_iso, int(sp_away_price), american_to_decimal(sp_away_price), "dk-screenshots")
                inserted_odds += 1
            if sp_home_line is not None and sp_home_price is not None:
                sel = ensure_market_selection(db, game_uuid, "spread", "full_game", home_id, "home", float(sp_home_line), DEFAULT_LINE_UNITS)
                if (not only_missing) or (only_missing and not db.has_odds_for_selection(sel, sportsbook_id)):
                    db.insert_odds(sel, sportsbook_id, now_iso, int(sp_home_price), american_to_decimal(sp_home_price), "dk-screenshots")
                inserted_odds += 1

            totals = (spread_container.get("total_goals") if isinstance(spread_container, dict) else None) or g.get("total_goals") or {}
            tnum = totals.get("number")
            t_odds_container = spread_container if isinstance(spread_container, dict) else g
            t_odds = t_odds_container.get("total_goals_odds") or {}
            over_p = _val(t_odds.get("over"))
            under_p = _val(t_odds.get("under"))
            if tnum is not None and over_p is not None:
                sel = ensure_market_selection(db, game_uuid, "total", "full_game", None, "over", float(tnum), DEFAULT_LINE_UNITS)
                if (not only_missing) or (only_missing and not db.has_odds_for_selection(sel, sportsbook_id)):
                    db.insert_odds(sel, sportsbook_id, now_iso, int(over_p), american_to_decimal(over_p), "dk-screenshots")
                inserted_odds += 1
            if tnum is not None and under_p is not None:
                sel = ensure_market_selection(db, game_uuid, "total", "full_game", None, "under", float(tnum), DEFAULT_LINE_UNITS)
                if (not only_missing) or (only_missing and not db.has_odds_for_selection(sel, sportsbook_id)):
                    db.insert_odds(sel, sportsbook_id, now_iso, int(under_p), american_to_decimal(under_p), "dk-screenshots")
                inserted_odds += 1

        except Exception as exc:
            print(f"[WARN] game processing error: {exc}")
            continue

    return {"inserted": inserted_odds}


def main() -> int:
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--selection", choices=["today", "tomorrow"], default="today")
    ap.add_argument("--mode", choices=["cards", "chunks"], default="chunks")
    ap.add_argument("--selector", default=None)
    ap.add_argument("--width", type=int, default=1400)
    ap.add_argument("--height", type=int, default=2000)
    ap.add_argument("--chunk_height", type=int, default=900)
    ap.add_argument("--overlap", type=int, default=220)
    ap.add_argument("--scale", type=int, default=4)
    ap.add_argument("--css_zoom", type=float, default=1.4, help="Apply CSS zoom to enlarge page content before screenshots (e.g., 1.4)")
    ap.add_argument("--teams", default=str(Path(__file__).parent / "teams_rows.json"))
    ap.add_argument("--example", default=str(Path(__file__).parent / "llmjsonoutput.json"))
    ap.add_argument("--reload", default=None, help="Path to an existing llmjsonoutput_*.json to reload instead of calling the LLM")
    ap.add_argument("--debug_raw", default=None)
    ap.add_argument("--outdir", default=str(Path(__file__).parent / "artifacts" / "screenshots" / f"dk_caps_{now_ts()}"))
    ap.add_argument("--dry_run", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--only_missing", action="store_true", help="Only insert odds where the selection has no existing odds for this sportsbook")
    ap.add_argument("--only_games", nargs="*", default=None, help="Optional list of game_id slugs to restrict updates to (AWAY_HOME_YYYY-MM-DD)")
    args = ap.parse_args()

    outdir = Path(args.outdir)
    ensure_dir(outdir)

    teams_path = Path(args.teams)
    example_path = Path(args.example)
    if not teams_path.exists():
        if args.reload:
            teams_rows = []  # allow reload mode to proceed without teams file when IDs are present
        else:
            raise RuntimeError(f"Missing teams_rows.json at {teams_path}")
    else:
        teams_rows = json.loads(teams_path.read_text())
    if not example_path.exists():
        if args.reload:
            llm_example = {"games": []}
        else:
            raise RuntimeError(f"Missing llmjsonoutput.json at {example_path}")
    else:
        llm_example = json.loads(example_path.read_text())

    images: List[Path] = []
    if not args.reload:
        with sync_playwright() as p:
            # channel="chromium" selects Chromium's *new* headless mode (full browser
            # binary) instead of the stripped-down "headless shell". As of 2026-09-19
            # DraftKings' Akamai bot manager 403s the headless shell but serves the
            # new-headless build normally. Requires `playwright install chromium`.
            browser = p.chromium.launch(
                headless=True,
                channel="chromium",
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                viewport={"width": args.width, "height": args.height},
                device_scale_factor=args.scale,
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/148.0.0.0 Safari/537.36"
                ),
            )
            page = context.new_page()
            for tag, url in DK_PAGES:
                print(f"Navigating… [{tag}] {url}")
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=10000)
                    wait_page_ready(page)
                    dismiss_modals(page)
                    if args.css_zoom and args.css_zoom != 1.0:
                        apply_css_zoom(page, float(args.css_zoom))
                    autoscroll_all(page)
                except Exception:
                    pass

                if args.mode == "chunks":
                    images += screenshot_chunks(page, outdir, width=args.width, chunk_h=args.chunk_height, overlap=args.overlap, prefix=tag)
                else:
                    images += screenshot_cards(page, args.selector, outdir)
            browser.close()

        if images:
            print("\nScreenshots:")
            for pth in images:
                print("-", pth)

    debug_path = Path(args.debug_raw) if args.debug_raw else None
    if args.reload:
        # Load existing JSON file instead of calling the model
        model_output = json.loads(Path(args.reload).read_text())
        # Remove duplicates just in case
        model_output = _dedupe_model_output(model_output, verbose=args.verbose)
    else:
        model_output = call_openai(images, teams_rows, llm_example, args.selection, debug_path=debug_path)
        model_output = _dedupe_model_output(model_output, verbose=args.verbose)

    out_json = Path(__file__).parent / f"artifacts/json/llmjsonoutput_{now_ts()}.json"
    out_json.write_text(json.dumps(model_output, indent=2))
    print(f"Wrote {out_json}")

    if args.dry_run:
        print("[DRY RUN] Skipping DB inserts.")
        return 0

    db = get_db()
    # Optional scoping by game slugs
    if args.only_games:
        # Filter model_output to only the specified game_ids
        games = model_output.get("games") or []
        model_output["games"] = [g for g in games if (g.get("game_id") in set(args.only_games))]
    res = ingest_from_json(db, model_output, teams_rows, verbose=args.verbose, only_missing=args.only_missing, only_games=args.only_games)
    print({"odds_inserted": res.get("inserted", 0)})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


