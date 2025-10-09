from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
from datetime import datetime, timezone
from pathlib import Path
import argparse, sys
import base64, json, pytz
from dateutil import parser as dtparser
import pandas as pd
from openai import OpenAI
from datetime import datetime, timedelta

LOCAL_TZ = pytz.timezone("America/Los_Angeles")
BOOK_NAME = "DraftKings"
SPORTSBOOK_ID = "4d7a01fa-2e62-48d2-aa87-41820bebf031"
URL = "https://sportsbook.draftkings.com/leagues/hockey/nhl"


def b64_data_url(png_path: str) -> str:
    b = Path(png_path).read_bytes()
    return "data:image/png;base64," + base64.b64encode(b).decode()


def load_team_map(csv_path: Path) -> dict:
    df = pd.read_csv(csv_path)
    # pick first id-ish column
    id_col = next((c for c in df.columns if "id" in c.lower()), None)
    name_cols = [c for c in df.columns if "name" in c.lower() or "alias" in c.lower()]
    if not id_col or not name_cols:
        raise RuntimeError(
            "teams_rows.csv must include an ID column and at least one name/alias column."
        )
    mapping = {}
    for _, r in df.iterrows():
        tid = str(r[id_col])
        for c in name_cols:
            val = str(r[c]).strip()
            if val and val.lower() != "nan":
                mapping[val.upper()] = tid
    return mapping


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


def american_to_decimal(american: int | None) -> float | None:
    if american is None:
        return None
    if isinstance(american, str) and american.strip().lower() == "ev":
        american = 100
    a = int(american)
    if a >= 100:
        return round(1 + a / 100.0, 2)
    if a <= -100:
        return round(1 + 100.0 / abs(a), 2)
    return None


def call_openai_extract(images: list[str], instruction_hint: str = "tomorrow", debug_path: str | None = None) -> dict:
    """
    Sends the screenshots to OpenAI Responses API (vision) and asks for
    a clean JSON with games (teams full names, local start time, ML/PL/Total odds).
    """
    client = OpenAI()  # uses OPENAI_API_KEY
    img_parts = [{"type": "input_image", "image_url": b64_data_url(p)} for p in images]

    system_prompt = (
        "You extract NHL betting lines from DraftKings screenshots. "
        "Return ONLY JSON. Do not invent numbers. "
        "Use FULL official team names (e.g., 'TAMPA BAY LIGHTNING'). "
        "Fields per game: away_team, home_team, start_time_local (as shown), "
        "moneyline: {away, home} in AMERICAN ints; "
        "puck_line: {away_line, away_price, home_line, home_price}; "
        "total_goals: {number, over_price, under_price}. "
        f"Only include games for {instruction_hint}."
    )

    # Try Responses API first; if SDK is older, gracefully fall back to Chat Completions
    raw = None
    try:
        resp = client.responses.create(
            model="gpt-5",
            response_format={"type": "json_object"},  # force JSON output
            instructions=system_prompt,
            input=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": "Parse these screenshots and output the games JSON.",
                        },
                        *img_parts,
                    ],
                }
            ],
        )
        raw = getattr(resp, "output_text", None)
    except TypeError:
        # Older openai SDK that doesn't support response_format/responses API
        pass
    except Exception:
        # Any other runtime/API error; we'll try a fallback path next
        pass

    if raw is None:
        # Fallback: Chat Completions with vision and JSON mode
        try:
            messages = [
                {"role": "system", "content": system_prompt},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Parse these screenshots and output the games JSON."},
                        *[
                            {"type": "image_url", "image_url": {"url": b64_data_url(p)}}
                            for p in images
                        ],
                    ],
                },
            ]
            resp2 = client.chat.completions.create(
                model="gpt-4o-mini",
                messages=messages,
                response_format={"type": "json_object"},
            )
            raw = resp2.choices[0].message.content if resp2 and resp2.choices else "{}"
        except Exception:
            raw = "{}"
    if debug_path:
        try:
            Path(debug_path).write_text(raw or "")
        except Exception:
            pass

    try:
        data = json.loads(raw)
    except Exception:
        # fallback: parse from first/last brace
        s = raw[raw.find("{") : raw.rfind("}") + 1]
        data = json.loads(s)
    return data


def call_agent_workflow(agent_or_workflow_id: str, images: list[str], selection: str = "today", debug_path: str | None = None) -> dict:
    """
    Run a published Agent Builder workflow using the Responses API with an agent_id.
    Sends screenshots as image inputs and requests strict JSON output.
    """
    client = OpenAI()
    img_parts = [{"type": "input_image", "image_url": b64_data_url(p)} for p in images]
    prompt = (
        "selection="
        + selection
        + "; Parse these screenshots of DraftKings NHL odds and return ONLY JSON."
    )

    raw = None
    try:
        # Accept either Agent Builder agent IDs or workflow IDs (wf_*)
        id_field = "workflow_id" if str(agent_or_workflow_id).startswith("wf_") else "agent_id"
        kwargs = {
            id_field: agent_or_workflow_id,
            "response_format": {"type": "json_object"},
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": prompt},
                        *img_parts,
                    ],
                }
            ],
        }
        resp = client.responses.create(**kwargs)
        raw = getattr(resp, "output_text", None)
    except Exception:
        # As a fallback, call the generic extractor (keeps script usable if Agent invocation fails)
        return call_openai_extract(images, instruction_hint=selection, debug_path=debug_path)
    if debug_path:
        try:
            Path(debug_path).write_text(raw or "")
        except Exception:
            pass

    try:
        return json.loads(raw or "{}")
    except Exception:
        s = (raw or "")[raw.find("{") : (raw or "").rfind("}") + 1]
        return json.loads(s or "{}")


def _find_games_in_structure(obj, depth: int = 0):
    if depth > 4:
        return []
    # Direct list of games (dicts with expected fields)
    if isinstance(obj, list):
        if obj and isinstance(obj[0], dict) and ("home_team" in obj[0] or "away_team" in obj[0]):
            return obj
        # search nested
        for it in obj:
            res = _find_games_in_structure(it, depth + 1)
            if res:
                return res
        return []
    if isinstance(obj, dict):
        # Common top-level containers
        if isinstance(obj.get("games"), list):
            return obj.get("games") or []
        for key in ("output_parsed", "final_output", "output", "data", "result", "response"):
            if key in obj:
                val = obj.get(key)
                # Sometimes nested JSON is a string
                if isinstance(val, str):
                    try:
                        val = json.loads(val)
                    except Exception:
                        pass
                res = _find_games_in_structure(val, depth + 1)
                if res:
                    return res
        # exhaustive search
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


def build_llmjson_from_extraction(extract: dict, team_map: dict) -> dict:
    """
    Post-process the LLM output:
    - normalize names,
    - map team IDs from teams_rows.csv,
    - round start times to :00/:30,
    - compute decimal odds,
    - format to llmjsonoutput.json shape.
    """
    out = {
        "time_zone": "America/Los_Angeles",
        "book": BOOK_NAME,
        "sportsbook_id": SPORTSBOOK_ID,
        "game_count": len(extract.get("games", [])),
        "games": [],
    }

    # Pull games from a variety of shapes the workflow might output
    games = None
    if isinstance(extract, dict):
        games = extract.get("games")
    if not isinstance(games, list):
        games = _find_games_in_structure(extract)
    if not isinstance(games, list):
        games = []
    for g in games:
        away = str(g["away_team"]).strip().upper()
        home = str(g["home_team"]).strip().upper()

        # map IDs with flexible match (try exact, then last two words)
        def map_id(name):
            tid = team_map.get(name)
            if tid:
                return tid
            parts = name.split()
            if len(parts) >= 2:
                short = " ".join(parts[-2:])
                tid = team_map.get(short)
                if tid:
                    return tid
            return None

        away_id = map_id(away)
        home_id = map_id(home)

        # parse and round local start time, then convert to UTC string
        dt_local = dtparser.parse(g["start_time_local"])
        dt_local = (
            LOCAL_TZ.localize(dt_local)
            if dt_local.tzinfo is None
            else dt_local.astimezone(LOCAL_TZ)
        )
        dt_local_rounded = round_to_half_hour(dt_local)
        dt_utc = dt_local_rounded.astimezone(pytz.UTC)

        # moneyline
        ml_away = int(g["moneyline"]["away"])
        ml_home = int(g["moneyline"]["home"])

        # puck line
        pl = g.get("puck_line", {})
        pl_away_line = (
            float(pl.get("away_line")) if pl.get("away_line") is not None else None
        )
        pl_home_line = (
            float(pl.get("home_line")) if pl.get("home_line") is not None else None
        )
        pl_away_price = (
            int(pl.get("away_price")) if pl.get("away_price") is not None else None
        )
        pl_home_price = (
            int(pl.get("home_price")) if pl.get("home_price") is not None else None
        )

        # totals
        tg = g.get("total_goals", {})
        total_num = float(tg["number"]) if tg.get("number") is not None else None
        over_price = (
            int(tg.get("over_price")) if tg.get("over_price") is not None else None
        )
        under_price = (
            int(tg.get("under_price")) if tg.get("under_price") is not None else None
        )

        game_obj = {
            "game_id": f"{(away.split()[-1][:3]).upper()}_{(home.split()[-1][:3]).upper()}_{dt_local_rounded.date().isoformat()}",
            "game_date": dt_local_rounded.date().isoformat(),
            "start_time": dt_utc.strftime("%Y-%m-%d %H:%M:%S+00"),
            "timestamp_utc": datetime.now(pytz.UTC).strftime("%Y-%m-%d %H:%M:%S.%f+00"),
            "away_team": away,
            "away_team_id": away_id,
            "home_team": home,
            "home_team_id": home_id,
            "market": {
                "moneyline": {
                    "away": {
                        "price_american": ml_away,
                        "price_european": american_to_decimal(ml_away),
                    },
                    "home": {
                        "price_american": ml_home,
                        "price_european": american_to_decimal(ml_home),
                    },
                },
                "spread": {"away": pl_away_line, "home": pl_home_line},
                "spread_odds": {
                    "away": {
                        "price_american": pl_away_price,
                        "price_european": american_to_decimal(pl_away_price),
                    },
                    "home": {
                        "price_american": pl_home_price,
                        "price_european": american_to_decimal(pl_home_price),
                    },
                },
                "total_goals": {"number": total_num},
                "total_goals_odds": {
                    "over": {
                        "price_american": over_price,
                        "price_european": american_to_decimal(over_price),
                    },
                    "under": {
                        "price_american": under_price,
                        "price_european": american_to_decimal(under_price),
                    },
                },
            },
        }
        out["games"].append(game_obj)
    return out


def ts():
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def dismiss_modals(page):
    # Try several common popups/banners
    for sel in [
        'button:has-text("Maybe later")',
        'button:has-text("No thanks")',
        'button:has-text("Not now")',
        'button:has-text("Got it")',
        'button[aria-label="Close"]',
        'button:has-text("Accept")'
    ]:
        try:
            page.locator(sel).first.click(timeout=1500)
        except Exception:
            pass


def wait_page_ready(page):
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_load_state("networkidle")
    # extra settle
    page.wait_for_timeout(1200)


def autoscroll_all(page, pause_ms=400):
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


def find_event_selector(page, user_selector=None):
    if user_selector:
        try:
            page.wait_for_selector(user_selector, timeout=5000)
            return user_selector
        except PwTimeout:
            pass

    # Try a bunch of real-world DK variants
    candidates = [
        # common DK classes/attrs seen across regions
        "div.sportsbook-event-accordion__event-row",
        "div.event-card",
        "div.sportsbook-event-card",
        "div[data-testid='event-row']",
        "[data-qa='event-row']",
        # often each 'row' has a 'More Bets' link inside
        "a:has-text('More Bets') >> xpath=ancestor::div[contains(@class,'event')]",
        "a:has-text('More Bets') >> xpath=ancestor::div[1]",
    ]
    for sel in candidates:
        try:
            page.wait_for_selector(sel, timeout=5000)
            return sel
        except PwTimeout:
            continue

    # dump HTML for debugging and return None
    Path("dk_debug_" + ts() + ".html").write_text(page.content())
    return None


def screenshot_cards(page, selector, outdir: Path):
    cards = page.locator(selector)
    count = cards.count()
    if count == 0:
        raise RuntimeError("Zero cards found for selector: " + selector)
    saved = []
    for i in range(count):
        el = cards.nth(i)
        try:
            el.scroll_into_view_if_needed()
            page.wait_for_timeout(150)
            fname = outdir / f"game_{i+1:02d}_{ts()}.png"
            el.screenshot(path=str(fname))
            saved.append(fname)
            print(f"Saved {fname.name}")
        except Exception as e:
            print(f"[WARN] card {i+1}: {e}")
    return saved


def screenshot_chunks(page, outdir: Path, width=1400, chunk_h=1600, overlap=220):
    page.set_viewport_size({"width": width, "height": chunk_h})
    total = page.evaluate("document.body.scrollHeight")
    y, idx, saved = 0, 1, []
    while y < total:
        page.evaluate(f"window.scrollTo(0, {y})")
        page.wait_for_timeout(250)
        fname = outdir / f"chunk_{idx:02d}_{ts()}.png"
        page.screenshot(path=str(fname), full_page=False)
        saved.append(fname)
        print(f"Saved {fname.name} (y={y})")
        y += chunk_h - overlap
        idx += 1
    page.evaluate("window.scrollTo(0, 0)")
    return saved


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--mode",
        choices=["cards", "chunks"],
        default="chunks",
        help="cards=one PNG per game, chunks=overlapping viewport (default).",
    )
    ap.add_argument("--outdir", default=f"dk_nhl_caps_{ts()}")
    ap.add_argument("--scale", type=int, default=3)
    ap.add_argument("--width", type=int, default=1400)
    ap.add_argument("--height", type=int, default=2000)
    ap.add_argument("--chunk_height", type=int, default=1600)
    ap.add_argument("--overlap", type=int, default=220)
    ap.add_argument(
        "--selector",
        default=None,
        help="Optional custom selector for per-card screenshots.",
    )
    ap.add_argument(
        "--agent_id",
        default=None,
        help="Run a published Agent Builder agent by ID to parse screenshots.",
    )
    ap.add_argument(
        "--workflow_id",
        default=None,
        help="Run a published Agent Builder workflow by ID (wf_...).",
    )
    ap.add_argument(
        "--selection",
        choices=["today", "tomorrow"],
        default="today",
        help="Which day's odds to extract and return.",
    )
    ap.add_argument(
        "--debug_raw",
        default=None,
        help="Optional path to write the raw model output for troubleshooting.",
    )
    args = ap.parse_args()

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True, args=["--disable-blink-features=AutomationControlled"]
        )
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
            page.goto(URL, wait_until="domcontentloaded", timeout=10000)
            wait_page_ready(page)
            dismiss_modals(page)
            autoscroll_all(page)  # ensure all events render
        except Exception:
            pass

        if args.mode == "chunks":
            files = screenshot_chunks(
                page,
                outdir,
                width=args.width,
                chunk_h=args.chunk_height,
                overlap=args.overlap,
            )
        else:
            sel = find_event_selector(page, user_selector=args.selector)
            if not sel:
                raise RuntimeError(
                    "Could not find event rows. Saved dk_debug_*.html for inspection."
                )
            files = screenshot_cards(page, sel, outdir)

        browser.close()

    print("\nDone. Files:")
    for f in files:
        print("-", f)

    images_for_vision = [str(p) for p in files]  # or filter/choose specific ones

    # Tell the model what to parse: "tomorrow" or "today"
    agent_or_workflow_id = args.workflow_id or args.agent_id
    if agent_or_workflow_id:
        llm_extract = call_agent_workflow(
            agent_or_workflow_id,
            images_for_vision,
            selection=args.selection,
            debug_path=args.debug_raw,
        )
    else:
        llm_extract = call_openai_extract(
            images_for_vision,
            instruction_hint=args.selection,
            debug_path=args.debug_raw,
        )

    # Post-process & validate against your teams_rows.csv
    team_map = load_team_map(
        Path("teams_rows.csv")
    )  # ensure this file sits next to the script
    final_json = build_llmjson_from_extraction(llm_extract, team_map)

    # Write output
    out_path = Path(f"llmjsonoutput_{ts()}.json")
    out_path.write_text(json.dumps(final_json, indent=2))
    print(f"Wrote {out_path} with {len(final_json['games'])} games.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("ERROR:", e)
        sys.exit(1)
